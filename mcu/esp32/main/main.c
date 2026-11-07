/**
 * @file    main.c
 * @brief   SerialPID-Tuner ESP32 固件入口：UART 接收任务 + 10ms 控制任务
 *
 * 与 STM32 版（mcu/Src/main_example.c）的关键差异 —— 也是本卡的第三、四点：
 *
 *   1) 没有裸机 while(1)。控制节拍由独立 FreeRTOS 任务用 vTaskDelayUntil 保证，
 *      10ms 控制 / 50ms 遥测。任务优先于日志，并钉在 core 1（core 0 留给 WiFi/BT
 *      协议栈，虽然本固件不开射频，但保持这个习惯方便以后叠加）。
 *   2) 接收不是"逐字节中断入环"。ESP-IDF 的 UART 驱动把 FIFO 中断折叠成事件：
 *      接收任务从事件队列拿到 UART_DATA，再 uart_read_bytes 批量取，
 *      整段交给 pid_app_on_rx_block（等价于连续调用 pid_app_on_rx_byte）。
 *      事件队列里同样只做入环，不解析、不打印、不阻塞 —— 与"中断里绝不阻塞"
 *      是同一条原则的两种实现形态。
 *   3) 时基 esp_timer_get_time()/1000，32 位毫秒回绕由 pid_app 内部无符号减法处理。
 */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#include "driver/uart.h"
#include "esp_timer.h"
#include "esp_log.h"

#include "pid_app.h"
#include "pid_calc.h"
#include "board_esp32.h"

#include <stdint.h>
#include <stdio.h>

#define TAG "app"

#define RX_TASK_STACK      4096
#define CTRL_TASK_STACK    6144
#define RX_TASK_PRIO       12      /* 高于控制任务：收字节不能被 10ms 控制拖出 ORE */
#define CTRL_TASK_PRIO     10
#define APP_CORE           1       /* core 0 是 WiFi/BT 的地盘，应用不跟它抢 */

#define RX_CHUNK           128     /* 一次最多批量取 128B（一帧最长 260B，多轮取完） */

static pid_app_t s_app;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ============================ 接收任务 ============================ */

static void rx_task(void *arg)
{
    uart_event_t ev;
    uint8_t      buf[RX_CHUNK];
    QueueHandle_t q = board_esp32_uart_queue();

    (void)arg;

    for (;;) {
        if (xQueueReceive(q, &ev, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        switch (ev.type) {
        case UART_DATA: {
            /* size 可能大于 RX_CHUNK，循环取干净，保证"不丢字节" */
            int remain = (int)ev.size;

            while (remain > 0) {
                int want = (remain > RX_CHUNK) ? RX_CHUNK : remain;
                int n    = uart_read_bytes(BOARD_UART_PORT, buf, (size_t)want, 0);

                if (n <= 0) {
                    break;
                }
                /* 唯一允许做的事：整段入环 */
                pid_app_on_rx_block(&s_app, buf, (uint16_t)n);
                remain -= n;
            }
            break;
        }
        case UART_FIFO_OVF:
        case UART_BUFFER_FULL:
            /* 溢出说明消费任务被长时间抢占：清空输入并重置事件队列，
             * 让协议从下一帧重新同步，而不是把半帧继续喂进解帧器 */
            uart_flush_input(BOARD_UART_PORT);
            xQueueReset(q);
            break;
        default:
            break;   /* UART_BREAK / PATTERN_DET 等：本协议不需要 */
        }
    }
}

/* ============================ 控制任务 ============================ */

static void ctrl_task(void *arg)
{
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(PID_CTRL_PERIOD_MS);
    uint32_t last_ms = now_ms();
    uint32_t tick    = 0u;

    (void)arg;

    for (;;) {
        uint32_t t   = now_ms();
        uint32_t gap = t - last_ms;

        last_ms = t;
        board_esp32_note_ctrl_interval((unsigned)gap);

        /* 解帧 + 分发（ACK / PONG / ERROR 都在里面按需发） */
        pid_app_poll(&s_app);

        /* 控制节拍 + 50ms 遥测 + 5s 延迟落 NVS */
        pid_app_tick(&s_app, t);

        /* 抖动量：每 5s 打一行，测量构建才开（BOARD_JITTER_LOG=1）。
         * 用 printf 而不是 ESP_LOGI，因为交付固件把日志级别压到了 ERROR。 */
#if BOARD_JITTER_LOG
        if ((++tick % (5000u / PID_CTRL_PERIOD_MS)) == 0u) {
            printf("CTRL jitter: max interval %u ms over %u ticks (target %u ms)\n",
                   (unsigned)board_esp32_ctrl_jitter_ms(), (unsigned)tick,
                   (unsigned)PID_CTRL_PERIOD_MS);
            fflush(stdout);
        }
#else
        (void)tick;
#endif

        vTaskDelayUntil(&last_wake, period);
    }
}

/* ============================ 入口 ============================ */

void app_main(void)
{
    const pid_app_io_t *io;
    pid_cfg_t           cfg;
    int                 rc;

    rc = board_esp32_init();
    if (rc != 0) {
        /* 初始化失败也要让上位机看得见：UART 若可用就打一行错误 */
        ESP_LOGE(TAG, "board init failed rc=%d", rc);
    }

    io = board_esp32_io();

    /* 默认参数与 STM32 示例保持一致，便于同一套上位机配置互相对照 */
    pid_cfg_default(&cfg);
    cfg.kp        = 1.5f;
    cfg.ki        = 3.0f;
    cfg.kd        = 0.05f;
    cfg.dt        = (float)PID_CTRL_PERIOD_MS / 1000.0f;
    cfg.out_min   = 0.0f;
    cfg.out_max   = 100.0f;
    cfg.mode      = PID_DEFAULT_MODE;
    cfg.d_on_pv   = true;
    cfg.d_lpf_tau = 0.02f;
    cfg.i_clamp   = true;

    pid_app_init(&s_app, io, &cfg);

    /* 先把接收任务挂起来，再开控制任务：先有耳朵，再有节拍 */
    if (xTaskCreatePinnedToCore(rx_task, "pid_rx", RX_TASK_STACK, NULL,
                                RX_TASK_PRIO, NULL, APP_CORE) != pdPASS) {
        ESP_LOGE(TAG, "rx task create failed");
        return;
    }
    if (xTaskCreatePinnedToCore(ctrl_task, "pid_ctrl", CTRL_TASK_STACK, NULL,
                                CTRL_TASK_PRIO, NULL, APP_CORE) != pdPASS) {
        ESP_LOGE(TAG, "ctrl task create failed");
        return;
    }

    ESP_LOGI(TAG, "SerialPID fw v%u ready (ctrl %ums / telemetry %ums)",
             (unsigned)PID_FW_VERSION, (unsigned)PID_CTRL_PERIOD_MS,
             (unsigned)PID_TELEMETRY_PERIOD_MS);
}
