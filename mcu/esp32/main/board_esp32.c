/**
 * @file    board_esp32.c
 * @brief   ESP32 板级六回调实现（UART / LEDC / ADC / NVS / esp_restart）
 *
 * 这是"接线图"在 ESP32 上的对应物，功能上与 mcu/Src/main_example.c 的
 * bsp_* 三段一一对应：
 *
 *   STM32（H750 参考板）           ESP32（本文件）
 *   HAL_UART_Transmit            → uart_write_bytes + uart_wait_tx_done
 *   HAL_UART_Receive_IT(逐字节)   → UART 事件队列 + uart_read_bytes 批量取
 *   TIM3_CH4 PWM 1kHz            → LEDC timer0 1kHz / 10bit（GPIO2）
 *   ADC1_IN10 单次转换            → adc_oneshot_read（GPIO34 / ADC1_CH6）
 *   Flash 扇区擦写 + CRC          → NVS blob + 同一份 pid_nvm_t
 *   NVIC_SystemReset             → esp_restart
 *
 * 所有回调都在控制任务或接收任务上下文里跑，**没有一句跑在中断里**：
 * ESP-IDF 的 UART 驱动把"中断"折叠成了事件队列，对应关系写在 README.md。
 */
#include "board_esp32.h"

#include "driver/uart.h"
#include "driver/ledc.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_err.h"

#include <math.h>
#include <string.h>

/* ============================ 编译开关 ============================ */

/**
 * 1（默认）= 一阶惯性仿真对象：PV 由 K=0.45 / T=8s 的模型产生，OUT 只驱动 LED，
 *            不接传感器也能把八步自测前三步跑通、也能看出闭环趋势。
 * 0        = 真实反馈：PV 读 GPIO34 的 ADC 电压（0..3.1V 映射成 0..100）。
 */
#ifndef BOARD_SIM_PLANT
#define BOARD_SIM_PLANT 1
#endif

/** 控制周期抖动打印开关在 board_esp32.h 里（BOARD_JITTER_LOG） */

#define BOARD_NVS_NAMESPACE "pids"
#define BOARD_NVS_KEY       "params"

#define BOARD_TAG "board"

/* ============================ 内部状态 ============================ */

static QueueHandle_t   s_uart_queue;
#if !BOARD_SIM_PLANT
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t          s_adc_cali;
static bool                       s_adc_cali_ok;
#endif
static pid_app_io_t               s_io;
static unsigned                   s_ctrl_jitter_ms;

#if BOARD_SIM_PLANT
static float    s_sim_pv;
static float    s_sim_out;
static uint32_t s_sim_last_ms;
#endif

/* ============================ 时基 ============================ */

static uint32_t board_now_ms(void)
{
    /* esp_timer 是 64 位微秒计数；应用层要的是 32 位毫秒，回绕由它自己处理 */
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* ============================ 输出：LEDC ============================ */

static void bsp_write_output(void *ctx, float out_percent)
{
    (void)ctx;

    if ((out_percent < 0.0f) || (out_percent > 100.0f)) {
        /* 应用层已限幅，这里再兜一层，绝不给执行器越界值 */
        out_percent = (out_percent < 0.0f) ? 0.0f : 100.0f;
    }

#if BOARD_SIM_PLANT
    s_sim_out = out_percent;
#endif

    {
        const float  full = (float)((1u << BOARD_LEDC_RES_BITS) - 1u);
        uint32_t     duty = (uint32_t)((out_percent * full) / 100.0f + 0.5f);

        if (duty > (uint32_t)full) {
            duty = (uint32_t)full;
        }
        /* 这里不查返回值：LEDC 配置在 init 里已校验过，运行期 set/update
         * 只可能因参数越界失败，而 duty 已被夹到合法区间 */
        (void)ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, duty);
        (void)ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0);
    }
}

/* ============================ 反馈：ADC 或仿真 ============================ */

static float bsp_read_pv(void *ctx)
{
    (void)ctx;

#if BOARD_SIM_PLANT
    /* 与 main_example.c 同一套模型：按真实经过时间积分，主循环快慢不影响时间常数 */
    {
        const float tau    = 8.0f;
        const float gain   = 0.45f;
        float       target = gain * s_sim_out;
        uint32_t    now    = board_now_ms();
        float       dt     = ((s_sim_last_ms == 0u)
                                  ? 0.0f
                                  : ((float)(now - s_sim_last_ms) / 1000.0f));

        if (dt > 0.05f) {
            dt = 0.05f;
        }
        s_sim_last_ms = now;

        s_sim_pv += (target - s_sim_pv) * (dt / tau);
    }
    return s_sim_pv;
#else
    {
        int raw = 0;

        if (s_adc == NULL) {
            return (float)NAN;   /* 未初始化：应用层保持上次输出 */
        }
        if (adc_oneshot_read(s_adc, BOARD_ADC_CHANNEL, &raw) != ESP_OK) {
            return (float)NAN;
        }

        if (s_adc_cali_ok && (s_adc_cali != NULL)) {
            int mv = 0;

            if (adc_cali_raw_to_voltage(s_adc_cali, raw, &mv) == ESP_OK) {
                /* 12dB 衰减的满量程约 3.1V；超出按端点夹住，避免负值/超量程 */
                float volt = (float)mv / 1000.0f;

                if (volt < 0.0f) {
                    volt = 0.0f;
                }
                if (volt > 3.1f) {
                    volt = 3.1f;
                }
                return (volt / 3.1f) * 100.0f;
            }
        }
        return ((float)raw / BOARD_ADC_FULL_SCALE) * 100.0f;
    }
#endif
}

/* ============================ 发送 ============================ */

static bool bsp_send_frame(void *ctx, const uint8_t *data, uint16_t len)
{
    (void)ctx;

    if ((data == NULL) || (len == 0u)) {
        return false;
    }

    /* 一帧最长 260B @115200 ≈ 22ms；遥测 50ms 一次，同步发送余量够。
     * 若把上报周期压到 <30ms，应改成环形发送缓冲 + 专门发送任务。 */
    if (uart_write_bytes(BOARD_UART_PORT, data, (size_t)len) < 0) {
        return false;
    }
    return uart_wait_tx_done(BOARD_UART_PORT, pdMS_TO_TICKS(20)) == ESP_OK;
}

/* ============================ NVM：NVS ============================ */

static bool bsp_nvm_load(void *ctx, pid_nvm_t *out)
{
    nvs_handle_t h;
    size_t       len = sizeof(*out);
    bool         ok  = false;

    (void)ctx;

    if (out == NULL) {
        return false;
    }
    if (nvs_open(BOARD_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        return false;   /* 命名空间还没建：等同"空 Flash" */
    }
    if ((nvs_get_blob(h, BOARD_NVS_KEY, out, &len) == ESP_OK) &&
        (len == sizeof(*out))) {
        /* 长度不符（旧版本结构变小/变大）一律当无效：宁可回默认参数，
         * 也不把错位的数据当参数用。magic 与 CRC 由 pid_app 再校验一遍。 */
        ok = true;
    }
    nvs_close(h);
    return ok;
}

static bool bsp_nvm_save(void *ctx, const pid_nvm_t *in)
{
    nvs_handle_t h;
    bool         ok = false;

    (void)ctx;

    if (in == NULL) {
        return false;
    }
    if (nvs_open(BOARD_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) {
        return false;
    }
    if (nvs_set_blob(h, BOARD_NVS_KEY, in, sizeof(*in)) == ESP_OK) {
        /* NVS 的 commit 是掉电安全的边界：不 commit 等于没写 */
        ok = (nvs_commit(h) == ESP_OK);
    }
    nvs_close(h);
    return ok;
}

/* ============================ 复位 ============================ */

static void bsp_system_reset(void *ctx)
{
    (void)ctx;
    esp_restart();
}

/* ============================ 初始化 ============================ */

static int board_init_nvs(void)
{
    esp_err_t err = nvs_flash_init();

    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /* 分区被旧固件写满或版本不符：擦一次重来，等同 STM32 侧"坏页 → 用默认参数" */
        if (nvs_flash_erase() != ESP_OK) {
            return -1;
        }
        err = nvs_flash_init();
    }
    return (err == ESP_OK) ? 0 : -1;
}

static int board_init_uart(void)
{
    uart_config_t cfg = {
        .baud_rate  = BOARD_UART_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    if (uart_driver_install(BOARD_UART_PORT, BOARD_UART_RX_BUF, 0,
                            BOARD_UART_EVT_QUEUE, &s_uart_queue, 0) != ESP_OK) {
        return -1;
    }
    if (uart_param_config(BOARD_UART_PORT, &cfg) != ESP_OK) {
        return -1;
    }
    /* UART0 的 TX/RX 默认就是 GPIO1/3，这里显式写一遍，换板子只改宏 */
    if (uart_set_pin(BOARD_UART_PORT, BOARD_UART_TX_PIN, BOARD_UART_RX_PIN,
                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK) {
        return -1;
    }
    return 0;
}

static int board_init_ledc(void)
{
    ledc_timer_config_t tcfg = {
        .speed_mode      = LEDC_LOW_SPEED_MODE,
        .duty_resolution = BOARD_LEDC_RES_BITS,
        .timer_num       = LEDC_TIMER_0,
        .freq_hz         = BOARD_LEDC_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    ledc_channel_config_t ccfg = {
        .gpio_num   = BOARD_LEDC_GPIO,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel    = LEDC_CHANNEL_0,
        .intr_type  = LEDC_INTR_DISABLE,
        .timer_sel  = LEDC_TIMER_0,
        .duty       = 0,
        .hpoint     = 0,
    };

    if (ledc_timer_config(&tcfg) != ESP_OK) {
        return -1;
    }
    if (ledc_channel_config(&ccfg) != ESP_OK) {
        return -1;
    }
    return 0;
}

static int board_init_adc(void)
{
#if BOARD_SIM_PLANT
    return 0;   /* 仿真对象不碰 ADC：没有传感器也能跑 */
#else
    adc_oneshot_unit_init_cfg_t ucfg = {
        .unit_id  = BOARD_ADC_UNIT,
        .clk_src  = ADC_RTC_CLK_SRC_DEFAULT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    adc_oneshot_chan_cfg_t ccfg = {
        .atten    = BOARD_ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_12,
    };

    if (adc_oneshot_new_unit(&ucfg, &s_adc) != ESP_OK) {
        return -1;
    }
    if (adc_oneshot_config_channel(s_adc, BOARD_ADC_CHANNEL, &ccfg) != ESP_OK) {
        return -1;
    }

    /* ESP32（非 S2/S3）只有 line_fitting 校准；创建失败就退回裸码映射，
     * 不影响"PV 会动"这件事，但百分量的绝对值精度差一些 */
    {
        adc_cali_line_fitting_config_t lcfg = {
            .unit_id  = BOARD_ADC_UNIT,
            .atten    = BOARD_ADC_ATTEN,
            .bitwidth = ADC_BITWIDTH_12,
        };

        if (adc_cali_create_scheme_line_fitting(&lcfg, &s_adc_cali) == ESP_OK) {
            s_adc_cali_ok = true;
        } else {
            s_adc_cali    = NULL;
            s_adc_cali_ok = false;
        }
    }
    return 0;
#endif
}

int board_esp32_init(void)
{
    int rc = 0;

    if (board_init_nvs() != 0) {
        ESP_LOGE(BOARD_TAG, "nvs init failed");
        rc = -1;
    }
    if (board_init_uart() != 0) {
        ESP_LOGE(BOARD_TAG, "uart init failed");
        rc = -2;
    }
    if (board_init_ledc() != 0) {
        ESP_LOGE(BOARD_TAG, "ledc init failed");
        rc = -3;
    }
    if (board_init_adc() != 0) {
        ESP_LOGE(BOARD_TAG, "adc init failed");
        rc = -4;
    }

    s_io.write_output = bsp_write_output;
    s_io.read_pv      = bsp_read_pv;
    s_io.send_frame   = bsp_send_frame;
    s_io.nvm_load     = bsp_nvm_load;
    s_io.nvm_save     = bsp_nvm_save;
    s_io.system_reset = bsp_system_reset;
    s_io.ctx          = NULL;

    return rc;
}

const pid_app_io_t *board_esp32_io(void)
{
    return &s_io;
}

QueueHandle_t board_esp32_uart_queue(void)
{
    return s_uart_queue;
}

/* ============================ 抖动记账 ============================ */

void board_esp32_note_ctrl_interval(unsigned interval_ms)
{
    /* 只记"最大的一次"，不记平均：平均值会被偶发的长间隔掩盖 */
    if (interval_ms > s_ctrl_jitter_ms) {
        s_ctrl_jitter_ms = interval_ms;
    }
}

unsigned board_esp32_ctrl_jitter_ms(void)
{
    return s_ctrl_jitter_ms;
}
