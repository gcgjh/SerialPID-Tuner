/**
 * @file    main_example.c
 * @brief   STM32 HAL 集成示例：主循环、UART 接收中断、1ms 时基、PWM 输出、ADC 采样
 *
 * 本文件是"接线图"，不是库的一部分：
 *   - pid_comm / pid_calc / pid_app 三个模块零 HAL 依赖
 *   - 只有本文件（和 pid_nvm_stm32_example.c）绑定具体外设
 *
 * ── 两种 IO 模式（编译宏 PID_DEMO_SIMULATED_PLANT 切换）────────────────────
 *   1（默认）仿真对象：PV 由一阶惯性模型产生，OUT 只写进内存变量，
 *                      **不会**驱动任何引脚。用于没有硬件的上位机联调。
 *   0        真实 IO ：OUT 走 TIM3_CH1 PWM，PV 走 ADC1 采样，
 *                      这才是"接到真实硬件"的那一半代码。
 *
 *   怎么确认板子里烧的是哪一种：断开执行器、给一个阶跃，若 PV 仍按 8s 时间常数
 *   平滑爬升且没有噪声，说明还在仿真模式（真实模式的 PV 会跟着传感器噪声跳）。
 *   详见 docs/wiring.md §3.3 与 docs/bringup.md 第 1 步。
 *
 * ── 使用前提（CubeMX 已生成，逐项对应见 docs/bringup.md §2）──────────────
 *   - USART1：115200 8N1，开启 NVIC 中断（优先级建议 3，低于控制/编码器中断）
 *   - 毫秒时基：默认用 SysTick（HAL_GetTick），CubeMX 默认就开着，不需要额外配；
 *     要用独立 TIM6 时定义 PID_EXAMPLE_USE_TIM6=1 并配好 1kHz 更新中断
 *     （注意 F103C8 等 F1 中容量型号没有 TIM6，此时只能用 SysTick 或 TIM4）
 *   - TIM3 ：PWM 输出，CH1，ARR=999（72MHz/72 分频 → 1kHz），PSC=71
 *   - ADC1 ：单次转换 + 软件触发，12 位，采样时间按信号源阻抗选（见 §2 表）
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
#include <math.h>      /* NAN：ADC 转换失败时用无效值哨兵，应用层据此保持上次输出 */

/* ============================ 可调示例参数 ============================ */

/** 1=用一阶惯性仿真对象（无硬件也能跑通上位机联调）；0=接真实传感器与执行器 */
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

/* ---- 真实 IO 参数（只在 PID_DEMO_SIMULATED_PLANT=0 时生效）---- */

/** TIM3 自动重装值：CubeMX > Timers > TIM3 > Parameter Settings > Counter Settings
 *  Prescaler = 71、Counter Period(ARR) = 999，72MHz 得到 1kHz PWM。 */
#ifndef PID_EXAMPLE_PWM_ARR
#define PID_EXAMPLE_PWM_ARR 999u
#endif

/** PWM 通道：CubeMX > Timers > TIM3 > Channel 选哪一个，就填对应 TIM_CHANNEL_x。
 *  默认 CH1（通用模板）；具体板子（如 H750VB 板载 TIM3_CH4/PB1）在 main.h 里覆盖。 */
#ifndef PID_EXAMPLE_PWM_CHANNEL
#define PID_EXAMPLE_PWM_CHANNEL TIM_CHANNEL_1
#endif

/** CCR 上限取 ARR+1：CCR > ARR 时输出恒为高，正好对应 100% 占空比 */
#define PID_EXAMPLE_PWM_CCR_MAX (PID_EXAMPLE_PWM_ARR + 1u)

/** ADC 满量程：12 位 = 4095。改成 1023 就是 10 位配置。 */
#ifndef PID_EXAMPLE_ADC_FULL_SCALE
#define PID_EXAMPLE_ADC_FULL_SCALE 4095.0f
#endif

/** 原始值 → 物理量的占位线性标定：默认把 0..4095 映射成 0..100（百分量程）。
 *  这只是"让曲线动起来"的占位，接真实传感器必须换成你的标定：
 *  例如 0..100℃ 的 PT100 调理输出 0.5..2.5V →
 *    PV_SCALE  = 100.0f / (2.5f - 0.5f) / (3.3f / 4095.0f)   （按你的分压比算）
 *    PV_OFFSET = -0.5f * PV_SCALE
 */
#ifndef PID_EXAMPLE_PV_SCALE
#define PID_EXAMPLE_PV_SCALE (100.0f / PID_EXAMPLE_ADC_FULL_SCALE)
#endif
#ifndef PID_EXAMPLE_PV_OFFSET
#define PID_EXAMPLE_PV_OFFSET 0.0f
#endif

/** 单次 ADC 转换的轮询超时（ms）。12 位 + 239.5 周期采样约 20µs，2ms 足够。 */
#ifndef PID_EXAMPLE_ADC_TIMEOUT_MS
#define PID_EXAMPLE_ADC_TIMEOUT_MS 2u
#endif

/** 无效测量哨兵：应用层用 pid_float_is_finite 判断，NaN 会让它保持上次输出 */
#if defined(NAN)
#define PID_EXAMPLE_PV_INVALID ((float)NAN)
#elif defined(__GNUC__)
#define PID_EXAMPLE_PV_INVALID (__builtin_nanf(""))
#else
#define PID_EXAMPLE_PV_INVALID (0.0f)   /* 兜底：退化成 0，仅限无 math.h 的编译器 */
#endif

extern UART_HandleTypeDef huart1;
extern TIM_HandleTypeDef  htim3;
#if !PID_DEMO_SIMULATED_PLANT
extern ADC_HandleTypeDef  hadc1;
#endif
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
/* ==========================================================================
 * 一、仿真对象（PID_DEMO_SIMULATED_PLANT=1）
 *
 * 一阶惯性对象：K=0.45，T=8s，pv 稳态 = 0.45*out。仅用于台架自测。
 * 这一整块不碰任何外设：写 OUT 只改内存变量，读 PV 只做积分。
 * ========================================================================== */

static float g_sim_pv = 0.0f;
static float g_sim_out = 0.0f;
static uint32_t g_sim_last_ms = 0u;   /**< 上次积分时刻；首帧为 0，表示还没开始计时 */

#endif /* PID_DEMO_SIMULATED_PLANT */

/* ============================ BSP：输出与测量 ============================ */

/**
 * @brief  输出通路：OUT(0..100%) → 执行器
 * @note   仿真模式下只记变量；真实模式下写 TIM3_CH1 的 CCR。
 */
static void bsp_write_output(void *ctx, float out_percent)
{
    (void)ctx;

    if ((out_percent < 0.0f) || (out_percent > 100.0f)) {
        /* 应用层已限幅，这里再兜一层，绝不给执行器越界值 */
        out_percent = (out_percent < 0.0f) ? 0.0f : 100.0f;
    }

#if PID_DEMO_SIMULATED_PLANT
    /* ---- 仿真：只写内存，不驱动任何引脚 ---- */
    g_sim_out = out_percent;
#else
    /* ---- 真实 IO：PWM 占空比 ----
     * CubeMX 对应项：Timers > TIM3 > Channel1 = PWM Generation CH1，
     *               PSC=71 / ARR=999（1kHz），引脚复用见 bringup.md §2.2。
     *
     * 极性：若你的驱动是低有效（如 PNP 三极管 / 低电平触发的 SSR），
     * 把 CCR 取反：ccr = PID_EXAMPLE_PWM_CCR_MAX - ccr; 并确认上电瞬间为"关"。
     * 半桥/推挽还要在定时器里配死区，本示例不涉及（单路 PWM 直驱）。
     */
    {
        uint32_t ccr = (uint32_t)((out_percent * (float)PID_EXAMPLE_PWM_CCR_MAX) / 100.0f + 0.5f);

        if (ccr > PID_EXAMPLE_PWM_CCR_MAX) {
            ccr = PID_EXAMPLE_PWM_CCR_MAX;
        }
        __HAL_TIM_SET_COMPARE(&htim3, PID_EXAMPLE_PWM_CHANNEL, ccr);
    }
#endif
}

/**
 * @brief  反馈通路：被控量 → PV
 * @note   仿真模式用一阶惯性积分；真实模式做一次 ADC 单次转换，
 *         失败（超时/启动失败）返回 NaN，应用层会自动保持上次输出。
 */
static float bsp_read_pv(void *ctx)
{
    (void)ctx;

#if PID_DEMO_SIMULATED_PLANT
    /* 按真实经过的时间积分，而不是"每次调用固定加 dt"。
     * pid_app_tick 在主循环里每圈都被调用（不是每 10 ms 一次），如果写死 dt=1ms，
     * 实际积分速度会随主循环速度漂移（本机主循环约百 kHz 级，模型会快几百倍）。
     * 用毫秒时基算差值，模型的时间常数才真的是 8 s，与主循环快慢无关。
     * 上限 50 ms 是防"断点暂停/Flash 擦写"造成一步跳变。 */
    {
        const float tau = 8.0f;
        const float gain = 0.45f;
        float target = gain * g_sim_out;
        uint32_t now = PID_EXAMPLE_NOW_MS();
        float dt = ((g_sim_last_ms == 0u) ? 0.0f
                                          : ((float)(now - g_sim_last_ms) / 1000.0f));

        if (dt > 0.05f) {
            dt = 0.05f;
        }
        g_sim_last_ms = now;

        g_sim_pv += (target - g_sim_pv) * (dt / tau);
    }
    return g_sim_pv;
#else
    /* ---- 真实 IO：ADC 单次转换（轮询）----
     * CubeMX 对应项：Analog > ADC1 > INx，
     *   Mode = Independent、Clock Prescaler 按主频选、
     *   Resolution = 12 bits、Scan Conversion Disable、
     *   Continuous Conversion Disable、External Trigger = Software Start、
     *   Data Alignment = Right、Sampling Time 按源阻抗选。
     *
     * 为什么用"每周期起一次 + 轮询"而不是连续转换：
     *   连续模式下 DR 一直在刷新，读到的是"最近一次"而非"本周期"的样本，
     *   而且转换出错时没有任何返回值可判；单次转换的 HAL 返回值本身就是
     *   可用的健康信号，代价只有 ~20µs，10ms 控制周期完全吃得下。
     */
    {
        uint32_t raw;

        if (HAL_ADC_Start(&hadc1) != HAL_OK) {
            return PID_EXAMPLE_PV_INVALID;          /* ADC 被占用/未初始化 */
        }
        if (HAL_ADC_PollForConversion(&hadc1, PID_EXAMPLE_ADC_TIMEOUT_MS) != HAL_OK) {
            (void)HAL_ADC_Stop(&hadc1);
            return PID_EXAMPLE_PV_INVALID;          /* 转换没完成：时钟/触发配置错 */
        }

        raw = HAL_ADC_GetValue(&hadc1);
        (void)HAL_ADC_Stop(&hadc1);

        return ((float)raw * PID_EXAMPLE_PV_SCALE) + PID_EXAMPLE_PV_OFFSET;
    }
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
 *         对应 docs/bringup.md 故障树第 1 类（无数据 / 单向下行）。
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
 * CubeMX 对应项：USART1 > DMA Settings > USART1_RX = DMA1_Channel5(Stream2/0)、
 *               Mode = Circular→Normal（Normal 更安全，回调里重挂）、
 *               NVIC 里 USART1 global 优先级高于 DMA 通道。
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

    /* --- 启动接收：先挂中断，再开时基 ---
     * CubeMX 对应项：Connectivity > USART1 > NVIC Settings >
     *               "USART1 global interrupt" 勾选，Preemption Priority = 3
     *               （优先级怎么定见 docs/bringup.md §2.3） */
    (void)HAL_UART_Receive_IT(&huart1, &g_rx_byte, 1u);
#if PID_EXAMPLE_USE_TIM6
    /* CubeMX 对应项：Timers > TIM6 > Mode = Activated，
     *               Prescaler = 71 / Counter Period = 999（1 kHz 更新中断），
     *               NVIC > "TIM6 DAC underrun" 勾选，抢占优先级 2 */
    HAL_TIM_Base_Start_IT(&htim6);
#endif

#if !PID_DEMO_SIMULATED_PLANT
    /* 真实 IO：开 PWM 通道。MX_TIM3_Init 只配好寄存器，通道必须显式 Start，
     * 少这一句的现象是"参数照常下发、ACK 正常、就是没有输出"。 */
    (void)HAL_TIM_PWM_Start(&htim3, PID_EXAMPLE_PWM_CHANNEL);
#endif
}

int main(void)
{
    HAL_Init();                /* HAL_Init 里定优先级分组 4 位抢占 / 0 位子优先级 */
    SystemClock_Config();      /* CubeMX 生成：时钟树对照 docs/bringup.md §2.1 */
    MX_GPIO_Init();
    MX_USART1_UART_Init();     /* CubeMX: 115200 8N1、无流控、8 倍过采样 */
#if PID_EXAMPLE_USE_TIM6
    MX_TIM6_Init();            /* 只有启用 TIM6 时基时才需要（F103C8 根本没有 TIM6） */
#endif
#if !PID_DEMO_SIMULATED_PLANT
    MX_TIM3_Init();            /* 只有真实 IO 模式才用 PWM；CubeMX: TIM3 CH1、PSC 见 §2.1、ARR=999 */
    MX_ADC1_Init();            /* 只有真实 IO 模式才要求工程里配了 ADC1 */
#endif

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
