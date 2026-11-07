/**
 * @file    board_esp32.h
 * @brief   ESP32 板级适配：UART / LEDC / ADC / NVS / 复位
 *
 * 这一层只做"外设 → pid_app_io_t 六回调"的翻译，不含任何协议或控制逻辑：
 *   write_output  → LEDC PWM（GPIO2 板载 LED，0..100% 占空比）
 *   read_pv       → SAR ADC（GPIO34，ADC1_CH6）或一阶惯性仿真值
 *   send_frame    → uart_write_bytes + wait_tx_done
 *   nvm_load/save → NVS blob（namespace "pids"，key "params"）
 *   system_reset  → esp_restart
 *
 * 与 STM32 版的差异只在实现形态：接收用 UART 事件队列批量取（不是逐字节中断），
 * 时基用 esp_timer_get_time（不是 SysTick），参数存 NVS（不是扇区擦写）。
 */
#ifndef BOARD_ESP32_H
#define BOARD_ESP32_H

#include "pid_app.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ============================ 引脚与口径 ============================ */

/** 协议串口：ESP32 开发板的 CH340 接在 UART0（GPIO1=TXD / GPIO3=RXD） */
#define BOARD_UART_PORT        0
#define BOARD_UART_TX_PIN      1
#define BOARD_UART_RX_PIN      3
#define BOARD_UART_BAUD        115200
#define BOARD_UART_RX_BUF      1024   /**< 驱动侧接收缓冲（字节） */
#define BOARD_UART_EVT_QUEUE   20     /**< 事件队列深度：满了说明消费任务被拖住 */

/** 输出：LEDC 通道，GPIO2 板载 LED（高电平点亮） */
#define BOARD_LEDC_GPIO        2
#define BOARD_LEDC_FREQ_HZ     1000
#define BOARD_LEDC_RES_BITS    LEDC_TIMER_10_BIT

/** 反馈：GPIO34 = ADC1_CH6（ESP32 上 34~39 只能做输入，正好当模拟输入） */
#define BOARD_ADC_UNIT         ADC_UNIT_1
#define BOARD_ADC_CHANNEL      ADC_CHANNEL_6
#define BOARD_ADC_ATTEN        ADC_ATTEN_DB_12
#define BOARD_ADC_FULL_SCALE   4095.0f

/**
 * 控制周期抖动打印：1 = 每 5s 打一行（只用于测量构建），
 * 0 = 静默（交付固件默认，避免文本夹进制帧流）。
 */
#ifndef BOARD_JITTER_LOG
#define BOARD_JITTER_LOG 0
#endif

/* ============================ 生命周期 ============================ */

/** @brief 初始化 UART / LEDC / ADC / NVS；失败返回错误码（调用方负责打印） */
int board_esp32_init(void);

/** @brief 取六回调（ctx 由本模块内部持有，调用方不要改） */
const pid_app_io_t *board_esp32_io(void);

/** @brief UART 驱动事件队列：接收任务从这里等 UART_DATA 事件 */
QueueHandle_t board_esp32_uart_queue(void);

/** @brief 取本次开机以来控制任务的最大周期偏差（ms），供抖动评估打印 */
unsigned board_esp32_ctrl_jitter_ms(void);

/** @brief 控制任务每圈把"相邻两圈的间隔"记进来（>=0 表示正常） */
void board_esp32_note_ctrl_interval(unsigned interval_ms);

#ifdef __cplusplus
}
#endif

#endif /* BOARD_ESP32_H */
