// ==========================================================================================
// 文件        : main.go
// 所属目录    : scripts/p2p_android_peer/
// 文件用途    : P2P 打洞验证的 Android 侧对端(纯 Go 静态二进制, 经 adb 推送到 /data/local/tmp 运行)
// 架构定位    : 一次性验证装置, 不属于产品代码; 不修改产品源码与契约
// 核心功能    : STUN 映射探测; NAT 过滤行为两阶段测试; 双向 UDP 打洞; 打洞成功后做链路吞吐测量
// 职责边界    : 只收发 32 字节探针包与吞吐包; 不写日志文件以外的任何位置; 不持久化任何凭据
// ==========================================================================================

package main

import (
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"sort"
	"time"
)

const (
	stunMagicCookie = 0x2112A442
	stunBindingReq  = 0x0001
	stunBindingResp = 0x0101
	packetMagic     = "RDP1"
	kindProbe       = 1
	kindAck         = 2
	kindBulk        = 3
)

type stunServerResult struct {
	Server string `json:"server"`
	RTTms  float64 `json:"rtt_ms"`
	Mapped string `json:"mapped"`
	Error  string `json:"error,omitempty"`
}

type result struct {
	Role            string             `json:"role"`
	BindIP          string             `json:"bind_ip"`
	LocalPort       int                `json:"local_port"`
	STUN            []stunServerResult `json:"stun"`
	Mapped          []string           `json:"mapped"`
	MappingBehavior string             `json:"mapping_behavior"`
	PeerAddr        string             `json:"peer_addr"`
	FilterPhaseRecv int                `json:"filter_phase_recv"`
	FilterPhaseSent int                `json:"filter_phase_sent"`
	PunchFirstMs    float64            `json:"punch_first_ms"`
	PunchRecv       int                `json:"punch_recv"`
	PunchSent       int                `json:"punch_sent"`
	PunchRTTp50     float64            `json:"punch_rtt_p50_ms"`
	PunchRTTp95     float64            `json:"punch_rtt_p95_ms"`
	PunchRTTMax     float64            `json:"punch_rtt_max_ms"`
	BulkRecvBytes   int64              `json:"bulk_recv_bytes"`
	BulkRecvPackets int                `json:"bulk_recv_packets"`
	BulkKbps        float64            `json:"bulk_kbps"`
	BulkSeconds     float64            `json:"bulk_seconds"`
	BulkFirstRecvMs float64            `json:"bulk_first_recv_ms"`
	Error           string             `json:"error,omitempty"`
}

func buildStunRequest() []byte {
	buf := make([]byte, 20)
	binary.BigEndian.PutUint16(buf[0:2], stunBindingReq)
	binary.BigEndian.PutUint16(buf[2:4], 0)
	binary.BigEndian.PutUint32(buf[4:8], stunMagicCookie)
	for i := 8; i < 20; i++ {
		buf[i] = byte(i * 7)
	}
	return buf
}

func parseStunResponse(data []byte) string {
	if len(data) < 20 {
		return ""
	}
	if binary.BigEndian.Uint16(data[0:2]) != stunBindingResp {
		return ""
	}
	offset := 20
	for offset+4 <= len(data) {
		attrType := binary.BigEndian.Uint16(data[offset : offset+2])
		attrLen := int(binary.BigEndian.Uint16(data[offset+2 : offset+4]))
		if offset+4+attrLen > len(data) {
			return ""
		}
		value := data[offset+4 : offset+4+attrLen]
		if attrType == 0x0020 && attrLen >= 8 && value[1] == 0x01 {
			port := binary.BigEndian.Uint16(value[2:4]) ^ 0x2112
			ip := make(net.IP, 4)
			cookie := uint32(stunMagicCookie)
			for i := 0; i < 4; i++ {
				ip[i] = value[4+i] ^ byte(cookie>>(24-8*uint(i)))
			}
			return fmt.Sprintf("%s:%d", ip.String(), port)
		}
		if attrType == 0x0001 && attrLen >= 8 && value[1] == 0x01 {
			port := binary.BigEndian.Uint16(value[2:4])
			return fmt.Sprintf("%s:%d", net.IP(value[4:8]).String(), port)
		}
		offset += 4 + attrLen + ((4 - attrLen%4) % 4)
	}
	return ""
}

func encodePacket(kind byte, seq uint32, ts uint64) []byte {
	buf := make([]byte, 32)
	copy(buf[0:4], packetMagic)
	buf[4] = kind
	binary.BigEndian.PutUint32(buf[5:9], seq)
	binary.BigEndian.PutUint64(buf[9:17], ts)
	return buf
}

func decodePacket(buf []byte) (byte, uint32, uint64, bool) {
	if len(buf) < 17 || string(buf[0:4]) != packetMagic {
		return 0, 0, 0, false
	}
	return buf[4], binary.BigEndian.Uint32(buf[5:9]), binary.BigEndian.Uint64(buf[9:17]), true
}

func classifyMapping(mapped []string) string {
	unique := map[string]bool{}
	for _, item := range mapped {
		if item != "" {
			unique[item] = true
		}
	}
	if len(unique) == 0 {
		return "unknown"
	}
	if len(unique) == 1 {
		return "endpoint-independent"
	}
	if len(unique) > 1 {
		return "address-or-port-dependent"
	}
	return "unknown"
}

func percentile(values []float64, ratio float64) float64 {
	if len(values) == 0 {
		return 0
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	index := int(float64(len(sorted)-1) * ratio)
	return sorted[index]
}

func main() {
	role := flag.String("role", "android", "对端角色: android 或 windows")
	bindIP := flag.String("bind-ip", "", "本地绑定地址, 留空表示全部接口")
	localPort := flag.Int("local-port", 41000, "本地 UDP 端口")
	stunList := flag.String("stun", "stun.miwifi.com:3478,stun.chat.bilibili.com:3478", "逗号分隔的 STUN 服务")
	exchangeIn := flag.String("exchange-in", "/sdcard/rdc_p2p/peer.txt", "对端映射地址文件")
	exchangeOut := flag.String("exchange-out", "/sdcard/rdc_p2p/me.json", "本端探测结果文件")
	peerOverride := flag.String("peer-addr", "", "直接指定对端地址, 跳过文件交换")
	outFile := flag.String("out", "/sdcard/rdc_p2p/result.json", "结果文件")
	waitSeconds := flag.Int("wait", 60, "等待对端映射地址的秒数")
	startDelay := flag.Int("start-delay", 3, "拿到对端地址后再等多少秒开始分阶段测试")
	phaseSeconds := flag.Int("phase", 4, "单阶段秒数")
	punchSeconds := flag.Int("punch", 16, "打洞秒数")
	bulkKbps := flag.Int("bulk-kbps", 0, "吞吐阶段发送码率, 0 表示不做")
	bulkSeconds := flag.Int("bulk", 10, "吞吐阶段秒数")
	flag.Parse()

	res := result{Role: *role, BindIP: *bindIP, LocalPort: *localPort}

	bindAddr := &net.UDPAddr{IP: net.IPv4zero, Port: *localPort}
	if *bindIP != "" {
		parsed := net.ParseIP(*bindIP)
		if parsed == nil {
			fail(res, *outFile, "绑定地址非法: "+*bindIP)
			return
		}
		bindAddr.IP = parsed
	}
	conn, err := net.ListenUDP("udp4", bindAddr)
	if err != nil {
		fail(res, *outFile, "监听失败: "+err.Error())
		return
	}
	defer conn.Close()
	res.LocalPort = conn.LocalAddr().(*net.UDPAddr).Port

	mapped := make([]string, 0, 4)
	for _, server := range splitComma(*stunList) {
		serverAddr, err := net.ResolveUDPAddr("udp4", server)
		if err != nil {
			res.STUN = append(res.STUN, stunServerResult{Server: server, Error: err.Error()})
			continue
		}
		started := time.Now()
		_, err = conn.WriteToUDP(buildStunRequest(), serverAddr)
		if err != nil {
			res.STUN = append(res.STUN, stunServerResult{Server: server, Error: err.Error()})
			continue
		}
		conn.SetReadDeadline(time.Now().Add(2500 * time.Millisecond))
		buffer := make([]byte, 2048)
		for {
			n, from, err := conn.ReadFromUDP(buffer)
			if err != nil {
				res.STUN = append(res.STUN, stunServerResult{Server: server, Error: err.Error()})
				break
			}
			addr := parseStunResponse(buffer[:n])
			if addr == "" {
				continue
			}
			elapsed := float64(time.Since(started).Microseconds()) / 1000.0
			res.STUN = append(res.STUN, stunServerResult{Server: server, RTTms: elapsed, Mapped: addr})
			mapped = append(mapped, addr)
			_ = from
			break
		}
	}
	res.Mapped = mapped
	res.MappingBehavior = classifyMapping(mapped)
	conn.SetReadDeadline(time.Time{})
	writeJSON(*exchangeOut, map[string]any{
		"role":     *role,
		"mapped":   mapped,
		"port":     res.LocalPort,
		"behavior": res.MappingBehavior,
		"stun":     res.STUN,
	})

	peerAddr := *peerOverride
	if peerAddr == "" {
		peerAddr = waitPeer(*exchangeIn, *waitSeconds)
	}
	if peerAddr == "" {
		fail(res, *outFile, "未拿到对端映射地址")
		return
	}
	res.PeerAddr = peerAddr
	peer, err := net.ResolveUDPAddr("udp4", peerAddr)
	if err != nil {
		fail(res, *outFile, "对端地址非法: "+peerAddr)
		return
	}
	time.Sleep(time.Duration(*startDelay) * time.Second)

	// 阶段一/二: NAT 过滤行为测试, 一端只发一端只听, 避免双方同时发包互相开洞
	phase := time.Duration(*phaseSeconds) * time.Second
	rttSamples := make([]float64, 0, 256)
	buffer := make([]byte, 2048)
	seq := uint32(0)
	if *role == "android" {
		res.FilterPhaseSent = sendProbes(conn, peer, &seq, phase)
	} else {
		res.FilterPhaseRecv = listenOnly(conn, peer, buffer, phase)
	}
	if *role == "android" {
		res.FilterPhaseRecv = listenOnly(conn, peer, buffer, phase)
	} else {
		res.FilterPhaseSent = sendProbes(conn, peer, &seq, phase)
	}

	// 阶段三: 双向打洞
	punchStart := time.Now()
	ended := punchStart.Add(time.Duration(*punchSeconds) * time.Second)
	nextSend := time.Now()
	for time.Now().Before(ended) {
		if !time.Now().Before(nextSend) {
			conn.WriteToUDP(encodePacket(kindProbe, seq, uint64(time.Now().UnixNano())), peer)
			seq++
			res.PunchSent++
			nextSend = nextSend.Add(100 * time.Millisecond)
		}
		conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
		n, from, err := conn.ReadFromUDP(buffer)
		if err != nil {
			continue
		}
		kind, pseq, pts, ok := decodePacket(buffer[:n])
		if !ok {
			continue
		}
		if res.PunchFirstMs == 0 {
			res.PunchFirstMs = float64(time.Since(punchStart).Microseconds()) / 1000.0
		}
		if kind == kindProbe {
			res.PunchRecv++
			conn.WriteToUDP(encodePacket(kindAck, pseq, pts), from)
		} else if kind == kindAck {
			rtt := float64(time.Now().UnixNano()-int64(pts)) / 1e6
			if rtt >= 0 && rtt < 10000 {
				rttSamples = append(rttSamples, rtt)
			}
		}
	}
	res.PunchRTTp50 = percentile(rttSamples, 0.5)
	res.PunchRTTp95 = percentile(rttSamples, 0.95)
	res.PunchRTTMax = percentile(rttSamples, 1.0)

	// 阶段四: 打洞成功后的链路吞吐
	if *bulkKbps > 0 && res.PunchRecv > 0 {
		res.BulkRecvBytes, res.BulkRecvPackets, res.BulkSeconds, res.BulkFirstRecvMs = bulkReceive(
			conn, buffer, time.Duration(*bulkSeconds)*time.Second)
		if res.BulkSeconds > 0 {
			res.BulkKbps = float64(res.BulkRecvBytes) * 8 / 1000.0 / res.BulkSeconds
		}
	}
	writeJSON(*outFile, res)
}

func fail(res result, outFile string, message string) {
	res.Error = message
	writeJSON(outFile, res)
	os.Exit(1)
}

func splitComma(value string) []string {
	items := make([]string, 0, 4)
	current := ""
	for _, char := range value {
		if char == ',' {
			if current != "" {
				items = append(items, current)
			}
			current = ""
			continue
		}
		current += string(char)
	}
	if current != "" {
		items = append(items, current)
	}
	return items
}

func waitPeer(path string, seconds int) string {
	deadline := time.Now().Add(time.Duration(seconds) * time.Second)
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(path)
		if err == nil {
			text := string(raw)
			cleaned := make([]byte, 0, len(text))
			for i := 0; i < len(text); i++ {
				char := text[i]
				if char != ' ' && char != '\n' && char != '\r' && char != '\t' {
					cleaned = append(cleaned, char)
				}
			}
			if len(cleaned) > 0 {
				return string(cleaned)
			}
		}
		time.Sleep(200 * time.Millisecond)
	}
	return ""
}

func sendProbes(conn *net.UDPConn, peer *net.UDPAddr, seq *uint32, duration time.Duration) int {
	sent := 0
	ended := time.Now().Add(duration)
	next := time.Now()
	for time.Now().Before(ended) {
		if !time.Now().Before(next) {
			conn.WriteToUDP(encodePacket(kindProbe, *seq, uint64(time.Now().UnixNano())), peer)
			*seq = *seq + 1
			sent++
			next = next.Add(200 * time.Millisecond)
		}
		time.Sleep(20 * time.Millisecond)
	}
	return sent
}

func listenOnly(conn *net.UDPConn, peer *net.UDPAddr, buffer []byte, duration time.Duration) int {
	received := 0
	ended := time.Now().Add(duration)
	for time.Now().Before(ended) {
		conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
		n, _, err := conn.ReadFromUDP(buffer)
		if err != nil {
			continue
		}
		if _, _, _, ok := decodePacket(buffer[:n]); ok {
			received++
		}
	}
	return received
}

func bulkReceive(conn *net.UDPConn, buffer []byte, duration time.Duration) (int64, int, float64, float64) {
	var total int64
	packets := 0
	started := time.Time{}
	firstMs := 0.0
	deadline := time.Now().Add(duration + 2*time.Second)
	for time.Now().Before(deadline) {
		conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
		n, _, err := conn.ReadFromUDP(buffer)
		if err != nil {
			if !started.IsZero() && time.Since(started) > 4*time.Second {
				break
			}
			continue
		}
		kind, _, _, ok := decodePacket(buffer[:n])
		if !ok || kind != kindBulk {
			continue
		}
		if started.IsZero() {
			started = time.Now()
		}
		firstMs = float64(time.Since(started).Microseconds()) / 1000.0
		total += int64(n)
		packets++
	}
	elapsed := 0.0
	if !started.IsZero() {
		elapsed = time.Since(started).Seconds()
	}
	return total, packets, elapsed, firstMs
}

func writeJSON(path string, value any) {
	raw, err := json.Marshal(value)
	if err != nil {
		fmt.Println(err.Error())
		return
	}
	_ = os.WriteFile(path, raw, 0o644)
	fmt.Println(string(raw))
}
