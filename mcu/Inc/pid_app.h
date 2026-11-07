/**
 * @file    pid_app.h
 * @brief   PID 应用层：帧分发 / 控制节拍 / 遥测上报 / 阶跃辨识 / 参数持久化
 *
 * 与硬件解耦：UART 发送、PWM 输出、传感器读取、Flash 读写全部通过
 * pid_app_io_t 注入，因此 F1/F4/H7 共用同一份应用代码。
 *
 * 时间基准：调用方把毫秒时基（HAL_GetTick / RTOS tick / 定时器计数）传进来，
 * 本模块内部所有节拍都用无符号减法，32 位回绕安全。
 */

#ifndef PID_APP_H
#define PID_APP_H

#include "pid_calc.h"
#include "pid_comm.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ============================ 可配置项 ============================ */

#ifndef PID_CTRL_PERIOD_MS
#define PID_CTRL_PERIOD_MS      10u   /**< 闭环控制周期(ms) */
#endif
#ifndef PID_TELEMETRY_PERIOD_MS
#define PID_TELEMETRY_PERIOD_MS 50u   /**< 遥测上报周期(ms)，协议建议 50~100 */
#endif
#ifndef PID_NVM_SAVE_DELAY_MS
#define PID_NVM_SAVE_DELAY_MS   5000u /**< 参数静默多久后落 Flash（防滑杆拖动磨损） */
#endif
/**
 * 固件版本（PONG.fw_ver 的唯一真相）：高 4 位主版本，低 4 位次版本，
 * 与仓库 tag 一一对应（规则见 docs/PROTOCOL.md §6）：
 *   v0.2.0 -> 0x02   v1.0.0 -> 0x10   v1.2.3 -> 0x12（补丁号不进 fw_ver）
 * 改这里的同时必须改 tag 与 CHANGELOG，三处任一漂移都会让用户报 bug时说不清版本。
 */
#ifndef PID_FW_VERSION
#define PID_FW_VERSION          2u
#endif
#ifndef PID_DEFAULT_MODE
#define PID_DEFAULT_MODE        PID_MODE_POSITIONAL
#endif
#ifndef PID_STEP_MAX_MS
#define PID_STEP_MAX_MS         60000u /**< 单次阶跃保持上限 */
#endif
#ifndef PID_STEP_MAX_AMP
#define PID_STEP_MAX_AMP        1000.0f
#endif

#define PID_NVM_MAGIC 0x50494431u /**< "PID1" */

/* ============================ 参数快照 ============================ */

/** 掉电保存内容（28 字节，无填充，可整体做 CRC） */
typedef struct {
    uint32_t magic;
    float    kp;
    float    ki;
    float    kd;
    float    sp;
    uint32_t mode;     /**< pid_mode_t */
    uint16_t crc;      /**< 前面所有字节的 CRC16-Modbus */
    uint16_t reserved;
} pid_nvm_t;

/* ============================ 平台接口 ============================ */

typedef struct {
    void  (*write_output)(void *ctx, float out);                        /**< 必选：输出到执行器 */
    float (*read_pv)(void *ctx);                                        /**< 必选：读测量值 */
    bool  (*send_frame)(void *ctx, const uint8_t *data, uint16_t len);  /**< 必选：发送原始帧 */
    bool  (*nvm_load)(void *ctx, pid_nvm_t *out);                       /**< 可选：读参数 */
    bool  (*nvm_save)(void *ctx, const pid_nvm_t *in);                  /**< 可选：写参数 */
    void  (*system_reset)(void *ctx);                                   /**< 可选：0x71 软复位 */
    void   *ctx;                                                        /**< 回调上下文 */
} pid_app_io_t;

/* ============================ 运行状态 ============================ */

typedef struct {
    pid_rb_t     rb;
    pid_parser_t parser;
    pid_ctrl_t   pid;
    pid_app_io_t io;

    float    sp;        /**< 当前设定值 */
    float    pv;        /**< 最近一次测量值 */
    float    out;       /**< 最近一次实际下发值（含阶跃测试偏移） */
    float    err;       /**< sp - pv */

    uint32_t now_ms;    /**< 最近一次 tick 的时基 */
    uint32_t t_ctrl;    /**< 上次控制时刻 */
    uint32_t t_tel;     /**< 上次上报时刻 */

    bool     nvm_dirty;
    uint32_t nvm_dirty_ms;
    bool     nvm_fail;

    bool     step_active;
    uint32_t step_t0;
    uint32_t step_hold;
    float    step_amp;
    float    step_base;

    uint32_t rx_frames;
    uint32_t tx_frames;
    uint32_t tx_bytes;
    uint32_t drop_bytes;
} pid_app_t;

/* ============================ 生命周期 ============================ */

/**
 * @brief  初始化应用（加载 Flash 参数，失败则用 cfg 默认）
 * @param  io  平台接口，必选三项(read_pv/write_output/send_frame)不能为 NULL
 * @param  cfg 默认 PID 配置，可为 NULL（用 pid_cfg_default）
 */
void pid_app_init(pid_app_t *app, const pid_app_io_t *io, const pid_cfg_t *cfg);

/** 中断/回调里调用：只入环形缓冲，不做任何解析 */
void pid_app_on_rx_byte(pid_app_t *app, uint8_t byte);

/** DMA/IDLE 批量接收路径：整段入环（等价于连续调用 on_rx_byte） */
void pid_app_on_rx_block(pid_app_t *app, const uint8_t *data, uint16_t len);

/** 主循环：解帧 + 分发（不阻塞、不动态分配） */
void pid_app_poll(pid_app_t *app);

/** 主循环每圈调用：控制节拍、遥测上报、延迟落 Flash */
void pid_app_tick(pid_app_t *app, uint32_t now_ms);

/** 停机：退出阶跃测试、输出置安全值（默认 out_min）、清控制状态 */
void pid_app_stop(pid_app_t *app);

#ifdef __cplusplus
}
#endif

#endif /* PID_APP_H */
