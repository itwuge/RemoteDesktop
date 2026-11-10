// ==========================================================================================
// 文件        : CaptureContract.kt
// 所属目录    : android/app/src/main/java/com/rdc/remote/
// 文件用途    : 采集与桌面契约之间的唯一接缝; 当前为占位实现
// 架构定位    : Android 采集适配层与命令层的边界
// 核心功能    : 接收一帧的宽高与时间戳; 返回是否被下游接收
// 职责边界    : 不编码、不传输、不落盘; 不写业务判断
// 运行环境    : 与桌面契约 desktop-v1 同版本约束
// 依赖关系    : 被 CaptureService 调用; 后续由 Chaquopy Python 核心实现
// ==========================================================================================

package com.rdc.remote

import android.util.Log

/**
 * 采集接缝.
 *
 * 现状: 只校验入参并丢弃帧, 用于验证「能装能起、采集链路通到接缝」;
 * 契约未定义帧级入口 (见 .team/records/契约变更记录 第十节 第 4 项), 因此这里不做编码与发送。
 */
internal object CaptureContract {

    private const val TAG = "RdcCaptureContract"

    /** 下游是否已接上. 当前恒为 false, 表示没有编码与发送. */
    const val WIRED = false

    /**
     * 提交一帧元数据.
     *
     * @param width 帧宽, 像素
     * @param height 帧高, 像素
     * @param timestampNs 系统时间戳, 纳秒
     * @return 是否被下游接收; 当前恒为 false
     */
    fun submitFrame(width: Int, height: Int, timestampNs: Long): Boolean {
        if (width <= 0 || height <= 0) {
            Log.w(TAG, "丢弃异常帧: ${width}x$height")
            return false
        }
        return WIRED
    }

    /**
     * 提交屏幕方向与尺寸变化, 供上层映射远端坐标系.
     *
     * @param width 采集面宽, 像素
     * @param height 采集面高, 像素
     * @param rotationDegrees 旋转角度, 0/90/180/270
     * @return 是否被下游接收; 当前恒为 false
     */
    fun submitDisplayState(width: Int, height: Int, rotationDegrees: Int): Boolean {
        if (width <= 0 || height <= 0 || rotationDegrees !in setOf(0, 90, 180, 270)) {
            Log.w(TAG, "忽略异常方向状态: ${width}x${height} rotation=$rotationDegrees")
            return false
        }
        Log.i(TAG, "方向状态: ${width}x${height} rotation=$rotationDegrees")
        return WIRED
    }
}
