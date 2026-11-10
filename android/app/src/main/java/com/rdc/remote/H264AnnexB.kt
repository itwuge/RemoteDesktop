// ==========================================================================================
// 文件        : H264AnnexB.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : Annex B 码流的 SPS/PPS 参数集提取
// 架构定位    : 媒体适配层纯函数; 从 RemoteRenderer 抽出以便无设备时做编码级单测
// 核心功能    : 同时识别 3/4 字节起始码; 只取 NAL 类型 7/8; 保留起始码供 MediaCodec csd 使用
// 职责边界    : 不解码、不判断分辨率、不持有状态; 非法/截断输入返回空结果而不抛异常
// 运行环境    : Android 10 (API 29) 至 Android 15 (API 35); JVM 单测
// 依赖关系    : 被 RemoteRenderer 调用
// ==========================================================================================

package com.rdc.remote

internal object H264AnnexB {

    data class ParamSets(val sps: ByteArray?, val pps: ByteArray?)

    /** 从一帧 Annex B 数据里取首个 SPS 与首个 PPS; 找不到时对应字段为 null. */
    fun extractParamSets(data: ByteArray): ParamSets {
        var sps: ByteArray? = null
        var pps: ByteArray? = null
        var index = 0
        var currentStart = -1
        var headerLength = 0
        while (index <= data.size - 3) {
            val startLength = when {
                data[index].toInt() == 0 && data[index + 1].toInt() == 0 && data[index + 2].toInt() == 1 -> 3
                index <= data.size - 4 && data[index].toInt() == 0 && data[index + 1].toInt() == 0 &&
                    data[index + 2].toInt() == 0 && data[index + 3].toInt() == 1 -> 4
                else -> 0
            }
            if (startLength > 0) {
                if (currentStart >= 0) {
                    val nal = sliceNal(data, currentStart, headerLength, index)
                    if (nal != null) {
                        when (nal.first) {
                            7 -> if (sps == null) sps = nal.second
                            8 -> if (pps == null) pps = nal.second
                        }
                    }
                }
                currentStart = index
                headerLength = startLength
                index += startLength
            } else {
                index++
            }
        }
        if (currentStart >= 0) {
            val nal = sliceNal(data, currentStart, headerLength, data.size)
            if (nal != null) {
                when (nal.first) {
                    7 -> if (sps == null) sps = nal.second
                    8 -> if (pps == null) pps = nal.second
                }
            }
        }
        return ParamSets(sps, pps)
    }

    /** 返回 (NAL 类型, 含起始码的字节), 头部缺失或空 NAL 返回 null. */
    private fun sliceNal(data: ByteArray, start: Int, headerLength: Int, end: Int): Pair<Int, ByteArray>? {
        val header = start + headerLength
        if (header >= data.size || end <= header) {
            return null
        }
        return (data[header].toInt() and 0x1f) to data.copyOfRange(start, end)
    }
}
