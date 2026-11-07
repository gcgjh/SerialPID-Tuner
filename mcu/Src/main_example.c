/**
 * @file    main_example.c
 * @brief   STM32 HAL 集成示例：主循环、UART 接收中断、1ms 定时器、PWM 输出
 *
 * 本文件是"接线图"，不是库的一部分：
 *   - pid_comm / pid_calc / pid_app 三个模块零 HAL 依赖
 *   - 只有本文件（和 pid_nvm_stm32_example.c）绑定具体外设
 *
 * 使用前提（CubeMX 已生成）：
 *   - USART1：115200 8N1，开启 NVIC 中断（优先级建议 3，低于控制/编码器中断）
 *   - 毫秒时基：默认用 SysTick（HAL_GetTick），CubeMX 默认就开着，不需要额外配；
 *     要用独立 TIM6 时定义 PID_EXAMPLE_USE_TIM6=1 并配好 1kHz 更新中断
 *     （注意 F103C8 等 F1 中容量型号没有 TIM6，此时只能用 SysTick 或 TIM4）
 *   - TIM3 ：PWM 输出（示例留了 TODO，按实际通道改）
 *   - ADC ：采样被控量（示例默认用一阶仿真对象，便于无硬件联调）
 *
 * 关键时序：UART 中断只做 pid_app_on_rx_byte（一次入环），
 *           解析、控制、组帧、发送全部在主循环，中断里绝不阻塞。
 */

/* ---- HAL 头文件：真实工程用 CubeMX 的 main.h，仓库回归用 mcu/stubs 下的桩 ----
 *
 * 正常使用（粘进自己的 CubeMX 工程）什么都不用定义：工程里一定有 main.h。
 * 仓库内编译验证时没有 CubeMX 工程，验证脚本会传 -DPID_HAL_STUB=1 并加上
 * mcu/stubs 到 include 路径，此时走桩头文件。
 *
 * PID_HAL_STUB 有意做成"三态"：不定义=自动探测，1=强制桩，0=强制真实 HAL。
 * 强制项存在的意义：桩与真实 HAL 必须能分别验证，否则签名漂移测不出来。
 */
#if !defined(PID_HAL_STUB)
#  if defined(__has_include)
#    if __has_include("main.h")
#      include "main.h"
#    else
#      include "stm32_hal_stub.h"
#    endif
#  else
#    include "main.h"          /* 老编译器没有 __has_include，按真实工程处理 */
#  endif
#elif PID_HAL_STUB
#  include "stm32_hal_stub.h"
#else
#  include "main.h"
#endif

#include "pid_app.h"
#include "pid_calc.h"
#include "pid_comm.h"

#include <stdint.h>

/* ============================ 可调示例参数 ============================ */

/** 1=用一阶惯性仿真对象（无硬件也能跑通上位机联调）；0=接真实传感器 */
#ifndef PID_DEMO_SIMULATED_PLANT
#define PID_DEMO_SIMULATED_PLANT 1
#endif

/**
 * 毫秒时基来源：
 *   0（默认）= SysTick（HAL_GetTick），全系 F1/F4/H7 都有；
 *   1        = TIM6 更新中断，需要 CubeMX 里配上 TIM6 并声明 htim6。
 *
 * 为什么默认改成 SysTick：本示例文档里点名的 F103C8 属于 F1 中容量，
 * 只有 TIM1~TIM4，**没有 TIM6**——原来写死 TIM6 的版本在目标芯片上编不过。
 * 需要独立 1ms 中断（比如 SysTick 被 RTOS 占了）时再打开这个开关。
 */
#ifndef PID_EXAMPLE_USE_TIM6
#define PID_EXAMPLE_USE_TIM6 0
#endif

#define EXAMPLE_BAUD_RATE 115200u

extern UART_HandleTypeDef huart1;
extern TIM_HandleTypeDef  htim3;
#if PID_EXAMPLE_USE_TIM6
extern TIM_HandleTypeDef  htim6;
#endif

/* ============================ 全局状态 ============================ */

static pid_app_t g_app;
static uint8_t   g_rx_byte;

#if PID_EXAMPLE_USE_TIM6
static volatile uint32_t g_ms_ticks = 0u;   /**< TIM6 1ms 计数，作为应用时基 */
#define PID_EXAMPLE_NOW_MS() (g_ms_ticks)
#else
#define PID_EXAMPLE_NOW_MS() (HAL_GetTick())   /**< SysTick 毫秒计数 */
#endif

#if PID_DEMO_SIMULATED_PLANT
/* 一阶惯性对象：K=0.45，T=8s，pv 稳态 = 0.45*out。仅用于台架自测。 */
static float g_sim_pv = 0.0f;
static float g_sim_out = 0.0f;
#endif

/* ============================ BSP：输出与测量 ============================ */

static void bsp_write_output(void *ctx, float out_percent)
{
    (void)ctx;

    if ((out_percent < 0.0f) || (out_percent > 100.0f)) {
        /* 应用层已限幅，这里再兜一层，绝不给执行器越界值 */
        out_percent = (out_percent < 0.0f) ? 0.0f : 100.0f;
    }

#if PID_DEMO_SIMULATED_PLANT
    g_sim_out = out_percent;
#else
    /* TODO: 换成实际 PWM。以 TIM3_CH1、ARR=999（1kHz）为例：
     *   uint32_t ccr = (uint32_t)(out_percent * 9.99f);
     *   __HAL_TIM_SET_COMPARE(&htim3, TIM_CHANNEL_1, ccr);
     * 若驱动的是加热丝/半桥，记得在这里处理极性、死区与安全上限。
     */
    (void)htim3;
#endif
}

static float bsp_read_pv(void *ctx)
{
    (void)ctx;

#if PID_DEMO_SIMULATED_PLANT
    /* 按 1ms 固定步长积分（调用频率约 1kHz，抖动对演示无影响） */
    {
        const float dt = 0.001f;
        const float tau = 8.0f;
        const float gain = 0.45f;
        float target = gain * g_sim_out;

        g_sim_pv += (target - g_sim_pv) * (dt / tau);
    }
    return g_sim_pv;
#else
    /* TODO: 换成实际采样。建议在这里做：
     *   1) ADC 原始值 -> 物理量（查表或线性标定）
     *   2) 一阶低通滤波（例如 y += (x-y)*0.1）
     *   3) 断线/超量程检测：返回 NaN 时应用层会自动保持上次输出
     */
    return 0.0f;
#endif
}

static bool bsp_send_frame(void *ctx, const uint8_t *data, uint16_t len)
{
    (void)ctx;

    /* 30 字节 @115200 约 2.6ms。遥测周期 50ms，轮询发送余量充足。
     * 若你把上报周期压到 <20ms，或波特率降到 57600，请改成 DMA/IT 发送：
     *   HAL_UART_Transmit_DMA(&huart1, (uint8_t *)data, len);
     * 并且要自己维护发送忙标志，避免上一帧未发完就覆盖缓冲。 */
    return HAL_UART_Transmit(&huart1, (uint8_t *)data, len, 20u) == HAL_OK;
}

/* ============================ 中断回调 ============================ */

/**
 * @brief  UART 接收完成：把 1 个字节塞进环形缓冲，然后立刻重新挂接收
 * @note   这里不要做任何解析、不要打印、不要等锁
 */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART1) {
        pid_app_on_rx_byte(&g_app, g_rx_byte);
        (void)HAL_UART_Receive_IT(&huart1, &g_rx_byte, 1u);
    }
}

/**
 * @brief  UART 错误（ORE 溢出最常见：主循环被长任务拖住时）
 * @note   HAL 在错误回调里已经把接收状态机打断了，必须 Abort + 重挂，
 *         否则表现为"能发不能收"，上位机侧看起来像设备掉线。
 */
void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART1) {
        __HAL_UART_CLEAR_OREFLAG(huart);
        __HAL_UART_CLEAR_NEFLAG(huart);
        __HAL_UART_CLEAR_FEFLAG(huart);
        (void)HAL_UART_AbortReceive_IT(huart);
        (void)HAL_UART_Receive_IT(&huart1, &g_rx_byte, 1u);
    }
}

/** 1ms 时基：默认走 SysTick，只有显式启用 TIM6 时才在这里累加 */
void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim)
{
#if PID_EXAMPLE_USE_TIM6
    if (htim->Instance == TIM6) {
        g_ms_ticks++;
    }
#else
    (void)htim;    /* 默认时基在 SysTick，本回调不被调用 */
#endif
}

/* ============================ 备选：DMA + IDLE 接收 ============================ */

#if 0
/*
 * 1 字节中断在 115200 下是 ~87us 一次中断，F1 也能扛，但占用偏多。
 * 更省 CPU 的做法是 DMA + IDLE（HAL_UARTEx_ReceiveToIdle_DMA）：
 *
 *   #define RX_DMA_LEN 128u
 *   static uint8_t g_dma_buf[RX_DMA_LEN];
 *
 *   启动（在初始化里）：
 *     HAL_UARTEx_ReceiveToIdle_DMA(&huart1, g_dma_buf, RX_DMA_LEN);
 *     __HAL_DMA_DISABLE_IT(huart1.hdmarx, DMA_IT_HT);   // 只关半传输中断
 *
 *   回调（收到 IDLE 或缓冲满时触发，Size = 本次收到的字节数）：
 *     void HAL_UARTEx_RxEventCallback(UART_HandleTypeDef *huart, uint16_t Size)
 *     {
 *         if (huart->Instance == USART1) {
 *             pid_app_on_rx_block(&g_app, g_dma_buf, Size);   // 整段入环
 *             HAL_UARTEx_ReceiveToIdle_DMA(&huart1, g_dma_buf, RX_DMA_LEN);
 *             __HAL_DMA_DISABLE_IT(huart1.hdmarx, DMA_IT_HT);
 *         }
 *     }
 *
 * 注意：每次 RxEvent 之后都必须重新启动 ReceiveToIdle_DMA，
 *       否则只会收到第一批数据（这是最常见的"偶发丢包"来源）。
 */
#endif

/* ============================ 初始化与主循环 ============================ */

void PID_Example_Init(void)
{
    pid_app_io_t io;
    pid_cfg_t    cfg;

    /* --- 平台接口 --- */
    io.write_output = bsp_write_output;
    io.read_pv      = bsp_read_pv;
    io.send_frame   = bsp_send_frame;
    io.nvm_load     = NULL;   /* 接了 pid_nvm_stm32_example.c 后再挂上 */
    io.nvm_save     = NULL;
    io.system_reset = NULL;   /* 需要 0x71 硬复位时挂 NVIC_SystemReset 包装 */
    io.ctx          = NULL;

    /* --- PID 默认参数（首次上电 / Flash 为空时生效）--- */
    pid_cfg_default(&cfg);
    cfg.kp = 1.5f;
    cfg.ki = 3.0f;
    cfg.kd = 0.05f;
    cfg.dt = (float)PID_CTRL_PERIOD_MS / 1000.0f;
    cfg.out_min = 0.0f;
    cfg.out_max = 100.0f;
    cfg.mode = PID_MODE_POSITIONAL;
    cfg.d_on_pv = true;
    cfg.d_lpf_tau = 0.02f;
    cfg.i_clamp = true;

    pid_app_init(&g_app, &io, &cfg);

    /* --- 启动接收：先挂中断，再开时基 --- */
    (void)HAL_UART_Receive_IT(&huart1, &g_rx_byte, 1u);
#if PID_EXAMPLE_USE_TIM6
    HAL_TIM_Base_Start_IT(&htim6);
#endif

    /* 若用 PWM：HAL_TIM_PWM_Start(&htim3, TIM_CHANNEL_1); */
}

int main(void)
{
    HAL_Init();
    SystemClock_Config();      /* CubeMX 生成 */
    MX_GPIO_Init();
    MX_USART1_UART_Init();
    MX_TIM6_Init();
    MX_TIM3_Init();

    PID_Example_Init();

    while (1) {
        /* 1) 解帧 + 分发（内部按需发送 ACK/PONG/ERROR） */
        pid_app_poll(&g_app);

        /* 2) 控制节拍 + 遥测 + 延迟落 Flash */
        pid_app_tick(&g_app, PID_EXAMPLE_NOW_MS());

        /* 3) 你自己的其他后台任务放这里，注意单次不要超过 ~5ms，
         *    否则 UART 1 字节中断可能 ORE 溢出 */
    }
}
