/**
 * @file    pid_comm.h
 * @brief   SerialPID-Tuner 串口协议层（MCU 侧）
 *
 * 协议基准：pc/serialpid/protocol.py（冻结，两侧必须逐字节一致）
 *
 *   0      1      2    3    4 ... 3+N      4+N    5+N    6+N ... 9+N
 *   0xAA   0x55   LEN  TYPE PAYLOAD...      CRC_LO CRC_HI 0x77 x4
 *
 *   - LEN  = payload 字节数（0..250）
 *   - CRC16-Modbus，poly 0xA001，init 0xFFFF，无输入/输出反射异或
 *   - CRC 校验范围 = LEN + TYPE + PAYLOAD（不含帧头、不含帧尾），小端附加
 *   - 所有多字节数值小端；float 为 IEEE-754 单精度小端
 *
 * 设计约束：
 *   - 本文件不依赖 HAL / CMSIS / RTOS，可在 F1/F4/H7 及 PC 上编译
 *   - 接收路径：UART 中断只做 pid_rb_push（单生产者，无锁）
 *   - 解析路径：主循环里 pid_comm_poll 出帧，回调中再做业务处理
 *
 * 编码：UTF-8（无 BOM）。Keil 用户请把工程 Encoding 设为 UTF-8。
 */

#ifndef PID_COMM_H
#define PID_COMM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ============================ 1. 帧常量 ============================ */

#define PID_SOF0              0xAAu  /**< 帧头字节 0 */
#define PID_SOF1              0x55u  /**< 帧头字节 1 */
#define PID_TAIL_BYTE         0x77u  /**< 帧尾字节（连续 4 个） */
#define PID_TAIL_LEN          4u     /**< 帧尾长度 */
#define PID_MAX_PAYLOAD       250u   /**< LEN 字段上限（u8 且留余量） */
#define PID_ERROR_MSG_MAX     64u    /**< ERROR 帧 ASCII 消息上限（省栈） */
#define PID_FRAME_OVERHEAD    10u    /**< SOF(2)+LEN(1)+TYPE(1)+CRC(2)+TAIL(4) */
#define PID_MAX_FRAME         (PID_MAX_PAYLOAD + PID_FRAME_OVERHEAD) /**< 260 */

/* ============================ 2. 包类型 ============================ */

typedef enum {
    PID_PKT_PING       = 0x01, /**< PC->MCU 空载荷，链路心跳 */
    PID_PKT_PONG       = 0x02, /**< MCU->PC fw_ver u8, status u8 */
    PID_PKT_PID_SET    = 0x10, /**< PC->MCU Kp,Ki,Kd,SP (4 x f32) */
    PID_PKT_PID_ACK    = 0x11, /**< MCU->PC 回显 Kp,Ki,Kd,SP,result u8 */
    PID_PKT_SP_SET     = 0x12, /**< PC->MCU SP f32 */
    PID_PKT_TELEMETRY  = 0x20, /**< MCU->PC t_ms u32, SP,PV,OUT,err f32 */
    PID_PKT_STEP_START = 0x30, /**< PC->MCU amplitude f32, hold_ms u32 */
    PID_PKT_STEP_STOP  = 0x31, /**< PC->MCU 空载荷 */
    PID_PKT_STEP_EVENT = 0x32, /**< MCU->PC phase u8, t_ms u32 */
    PID_PKT_ERROR      = 0x70, /**< MCU->PC code u8, msg_len u8, ASCII */
    PID_PKT_RESET      = 0x71  /**< PC->MCU 空载荷，软复位 MCU */
} pid_pkt_type_t;

/* PID_ACK 的 result 字段 */
#define PID_ACK_OK          0u
#define PID_ACK_BAD_PARAM   1u

/* PONG.status 取值（与 PC 侧 PongStatus 冻结一致：0 空闲 / 1 运行 / 2 阶跃中） */
#define PID_STATUS_IDLE      0u
#define PID_STATUS_RUNNING   1u
#define PID_STATUS_STEPPING  2u

/* STEP_EVENT.phase 取值（与 PC 侧 StepPhase 冻结一致：0 空闲 / 1 运行中 / 2 完成） */
#define PID_PHASE_IDLE       0u
#define PID_PHASE_RUNNING    1u
#define PID_PHASE_DONE       2u

/* ERROR 包的 code 字段 */
typedef enum {
    PID_ERR_NONE      = 0x00,
    PID_ERR_PARAM     = 0x01, /**< 参数非法（NaN/Inf/超范围） */
    PID_ERR_PAYLOAD   = 0x02, /**< 载荷长度与类型不符 */
    PID_ERR_TYPE      = 0x03, /**< 未知包类型 */
    PID_ERR_NVM       = 0x04, /**< 参数存储失败 */
    PID_ERR_STEP      = 0x05, /**< 阶跃测试参数非法/忙 */
    PID_ERR_INTERNAL  = 0x06
} pid_err_code_t;

/* ============================ 3. CRC16-Modbus ============================ */

/**
 * @brief  CRC16-Modbus（与 Python crc16_modbus 完全一致）
 * @param  data  数据首地址（允许 NULL，此时返回 init 值 0xFFFF）
 * @param  len   字节数
 * @retval u16 校验值
 */
uint16_t pid_crc16_modbus(const uint8_t *data, uint16_t len);

/* ============================ 4. 小端数值读写 ============================ */

void     pid_write_u16le(uint8_t *dst, uint16_t v);
uint16_t pid_read_u16le(const uint8_t *src);
void     pid_write_u32le(uint8_t *dst, uint32_t v);
uint32_t pid_read_u32le(const uint8_t *src);
void     pid_write_f32le(uint8_t *dst, float v);
float    pid_read_f32le(const uint8_t *src);

/* ============================ 5. 接收环形缓冲 ============================ */

/**
 * 单生产者（UART 中断）/单消费者（主循环）无锁环。
 * 索引为 uint16_t：Cortex-M 上 16 位对齐读写是单指令，天然原子。
 * head 只由中断写，tail 只由主循环写，无需关中断。
 */
#ifndef PID_RX_RING_SIZE
#define PID_RX_RING_SIZE 512u
#endif

#if ((PID_RX_RING_SIZE & (PID_RX_RING_SIZE - 1u)) != 0u)
#error "PID_RX_RING_SIZE 必须是 2 的幂（建议 256/512/1024）"
#endif
#if (PID_RX_RING_SIZE > 65535u)
#error "PID_RX_RING_SIZE 不得超过 65535"
#endif

typedef struct {
    uint8_t           buf[PID_RX_RING_SIZE];
    volatile uint16_t head;        /**< 生产者写指针（ISR 独占） */
    volatile uint16_t tail;        /**< 消费者读指针（主循环独占） */
    volatile uint32_t drop_count;  /**< 因满溢出丢弃的字节数 */
} pid_rb_t;

void     pid_rb_init(pid_rb_t *rb);

/**
 * @brief  ISR 中调用：压入一个字节
 * @retval true=成功 false=缓冲已满（该字节被丢弃并计入 drop_count）
 */
bool     pid_rb_push(pid_rb_t *rb, uint8_t byte);

/** @brief 当前缓存字节数（主循环调用） */
uint16_t pid_rb_count(const pid_rb_t *rb);

/**
 * @brief  主循环中调用：批量取出
 * @param  dst 目的缓冲
 * @param  max 最多取出字节数
 * @retval 实际取出字节数
 */
uint16_t pid_rb_read(pid_rb_t *rb, uint8_t *dst, uint16_t max);

/* ============================ 6. 解帧器 ============================ */

/** 解出的一帧（payload 指向解析器内部窗口，回调返回后即失效，勿保存指针） */
typedef struct {
    uint8_t        type;
    uint16_t       len;
    const uint8_t *payload;
} pid_frame_t;

/** 帧回调：user 为 pid_comm_poll 传入的上下文 */
typedef void (*pid_frame_cb_t)(const pid_frame_t *frame, void *user);

typedef struct {
    uint8_t  win[PID_MAX_FRAME]; /**< 解析窗口（最多容纳一整帧） */
    uint16_t len;                /**< 窗口内有效字节数 */
    uint32_t n_frames;           /**< 成功解出的帧数 */
    uint32_t err_crc;            /**< CRC 错误计数 */
    uint32_t err_format;         /**< 帧头/长度/帧尾格式错误计数 */
    uint32_t dropped_bytes;      /**< 为重新同步而丢弃的字节数 */
} pid_parser_t;

void     pid_parser_init(pid_parser_t *ps);

/**
 * @brief  喂入一段字节并尽量解出所有完整帧
 * @note   可在任意长度分片（1 字节也行），内部维护跨片状态
 * @retval 本次新解出的帧数
 */
uint16_t pid_parser_feed(pid_parser_t *ps, const uint8_t *src, uint16_t len,
                         pid_frame_cb_t cb, void *user);

/**
 * @brief  主循环入口：把环形缓冲抽干并解帧
 * @retval 本次新解出的帧数
 */
uint16_t pid_comm_poll(pid_parser_t *ps, pid_rb_t *rb,
                       pid_frame_cb_t cb, void *user);

/* ============================ 7. 组帧 ============================ */

/**
 * @brief  组装一帧
 * @param  out         输出缓冲，容量需 >= payload_len + PID_FRAME_OVERHEAD
 * @param  cap         输出缓冲容量
 * @param  payload     载荷（可为 NULL，当 payload_len==0）
 * @param  payload_len 载荷长度，> PID_MAX_PAYLOAD 时失败
 * @retval 帧总字节数；参数非法或缓冲不足返回 0
 */
uint16_t pid_frame_encode(uint8_t *out, uint16_t cap, uint8_t type,
                          const uint8_t *payload, uint16_t payload_len);

/* 便捷组帧：返回帧长（0=失败），out 容量建议 >= PID_MAX_FRAME */
uint16_t pid_build_pong(uint8_t *out, uint16_t cap,
                        uint8_t fw_ver, uint8_t status);

uint16_t pid_build_pid_ack(uint8_t *out, uint16_t cap,
                           float kp, float ki, float kd, float sp,
                           uint8_t result);

uint16_t pid_build_telemetry(uint8_t *out, uint16_t cap,
                             uint32_t t_ms, float sp, float pv,
                             float out_val, float err);

uint16_t pid_build_step_event(uint8_t *out, uint16_t cap,
                              uint8_t phase, uint32_t t_ms);

/**
 * @brief  错误帧：code u8, msg_len u8, ASCII
 * @note   msg 自动截断到 PID_ERROR_MSG_MAX 字节
 */
uint16_t pid_build_error(uint8_t *out, uint16_t cap,
                         uint8_t code, const char *msg);

#ifdef __cplusplus
}
#endif

#endif /* PID_COMM_H */
