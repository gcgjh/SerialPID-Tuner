/**
 * @file    stm32_hal_stub.h
 * @brief   最小 HAL 桩：只给类型、宏、函数声明，**不给任何实现**
 *
 * 用途：让 mcu/Src/main_example.c 与 mcu/Src/pid_nvm_stm32_example.c 在没有
 *      CubeMX 工程、没有芯片型号的情况下也能被 ARM 工具链编译一遍，从而把
 *      "语法错误、类型不匹配、HAL 签名漂移、寄存器宏改名"这类问题在回归阶段
 *      就拦住，而不是等用户照抄示例时才炸。
 *
 * 三条硬约束（改这个文件前先读）：
 *   1. 不许写实现。一旦这里出现函数体，示例就"看起来跑通了"，比不验证更危险。
 *      这里只有声明，所以本桩只用于**编译验证**（-c / -fsyntax-only），不参与链接。
 *   2. 签名要向真实 HAL 看齐。F1/F4 的关键符号已按 STM32Cube_FW_F1_V1.8.7、
 *      STM32Cube_FW_F4_V1.28.3 的实际头文件核对过；H7 无本地参考包，按公开头文件
 *      的签名写，属于"桩验证、未经真实 HAL 核对"（见 docs/wiring.md 的验证矩阵）。
 *   3. 族差异要保留。三个族可用哪些擦除/编程宏是不同的，`pid_nvm_stm32_example.c`
 *      正是靠这些宏选分支；桩里若把三族的宏都给全，分支选择就永远测不出来。
 *
 * 选择族：编译时定义 PID_HAL_STUB_FAMILY = 1 / 4 / 7（默认 1 = F1 系列）。
 *        编译验证脚本会分别用三个值各编一次。
 */

#ifndef STM32_HAL_STUB_H
#define STM32_HAL_STUB_H

#include <stdbool.h>
#include <stdint.h>

#ifndef PID_HAL_STUB_FAMILY
#define PID_HAL_STUB_FAMILY 1
#endif

#if (PID_HAL_STUB_FAMILY != 1) && (PID_HAL_STUB_FAMILY != 4) && (PID_HAL_STUB_FAMILY != 7)
#error "PID_HAL_STUB_FAMILY 只能是 1 / 4 / 7（分别对应 F1、F4、H7 系列）"
#endif

/* ============================ 基础类型 ============================ */

typedef enum {
    HAL_OK = 0x00U,
    HAL_ERROR = 0x01U,
    HAL_BUSY = 0x02U,
    HAL_TIMEOUT = 0x03U
} HAL_StatusTypeDef;

/** 外设寄存器块：示例只用到 Instance 指针比较，这里给个占位结构即可 */
typedef struct {
    volatile uint32_t SR;
    volatile uint32_t DR;
} USART_TypeDef;

typedef struct {
    volatile uint32_t CR1;
    volatile uint32_t SR;
} TIM_TypeDef;

typedef struct {
    volatile uint32_t ACR;
} FLASH_TypeDef;

/** 串口句柄：真实 HAL 的字段远多于此处，示例只用 Instance（hdmarx 仅出现在注释块） */
typedef struct {
    USART_TypeDef *Instance;
    void          *hdmarx;
    uint32_t       ErrorCode;
} UART_HandleTypeDef;

typedef struct {
    TIM_TypeDef *Instance;
    uint32_t     State;
} TIM_HandleTypeDef;

/** 外设实例：地址取真实基址，语义与 CubeMX 生成的一致 */
#define USART1 ((USART_TypeDef *)0x40013800UL)
#define TIM3   ((TIM_TypeDef *)0x40000400UL)
#define TIM6   ((TIM_TypeDef *)0x40001000UL)

/** Flash 擦除参数：三族字段并集（F1 用 PageAddress/NbPages，F4 用 Sector/NbSectors/
 *  VoltageRange，H7 额外用 Banks）。真实 HAL 各族的结构体也是这一族字段的并集。 */
typedef struct {
    uint32_t TypeErase;
    uint32_t Banks;
    uint32_t PageAddress;
    uint32_t NbPages;
    uint32_t Sector;
    uint32_t NbSectors;
    uint32_t VoltageRange;
} FLASH_EraseInitTypeDef;

/* ============================ 函数声明（无实现） ============================ */

HAL_StatusTypeDef HAL_Init(void);
void HAL_Delay(uint32_t Delay);
uint32_t HAL_GetTick(void);

HAL_StatusTypeDef HAL_UART_Transmit(UART_HandleTypeDef *huart, const uint8_t *pData,
                                    uint16_t Size, uint32_t Timeout);
HAL_StatusTypeDef HAL_UART_Receive_IT(UART_HandleTypeDef *huart, uint8_t *pData,
                                      uint16_t Size);
HAL_StatusTypeDef HAL_UART_AbortReceive_IT(UART_HandleTypeDef *huart);

HAL_StatusTypeDef HAL_TIM_Base_Start_IT(TIM_HandleTypeDef *htim);
HAL_StatusTypeDef HAL_TIM_PWM_Start(TIM_HandleTypeDef *htim, uint32_t Channel);

HAL_StatusTypeDef HAL_FLASH_Unlock(void);
HAL_StatusTypeDef HAL_FLASH_Lock(void);
HAL_StatusTypeDef HAL_FLASH_Program(uint32_t TypeProgram, uint32_t Address,
                                    uint64_t Data);
HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *pEraseInit,
                                    uint32_t *SectorError);

/* 中断回调由示例自己定义，真实 HAL 里是 __weak 声明 */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart);
void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart);
void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim);

/** CubeMX 生成的入口函数（桩里只有声明，示例可以照常调用） */
void SystemClock_Config(void);
void MX_GPIO_Init(void);
void MX_USART1_UART_Init(void);
void MX_TIM6_Init(void);
void MX_TIM3_Init(void);
void Error_Handler(void);

/* ============================ 寄存器宏 ============================ */

#define __HAL_UART_CLEAR_OREFLAG(__HANDLE__)  ((void)(__HANDLE__))
#define __HAL_UART_CLEAR_NEFLAG(__HANDLE__)   ((void)(__HANDLE__))
#define __HAL_UART_CLEAR_FEFLAG(__HANDLE__)   ((void)(__HANDLE__))
#define __HAL_TIM_SET_COMPARE(__HANDLE__, __CHANNEL__, __COMPARE__) \
    ((void)(__HANDLE__), (void)(__CHANNEL__), (void)(__COMPARE__))

#define __HAL_FLASH_CLEAR_FLAG(__FLAG__) ((void)(__FLAG__))

/* ============================ 族相关 Flash 符号 ============================ */

#if (PID_HAL_STUB_FAMILY == 1)
/* ---- F1/F0/L0：页擦除 + 半字编程 ---- */
#define FLASH_TYPEPROGRAM_HALFWORD 0x01U
#define FLASH_TYPEPROGRAM_WORD     0x02U
#define FLASH_TYPEPROGRAM_DOUBLEWORD 0x03U
#define FLASH_TYPEERASE_PAGES      0x00U
#define FLASH_TYPEERASE_MASSERASE  0x02U
#define FLASH_FLAG_EOP             0x00000001U
#define FLASH_FLAG_PGERR           0x00000004U
/* 注意：F1 的寄存器位叫 SR_WRPRTERR，但 HAL 宏叫 FLASH_FLAG_WRPERR（不是 WRPRTERR）。
 * 这里按 HAL 宏名给，写错名字在真实 HAL 下编不过——这条是本次验证抓出来的真问题。 */
#define FLASH_FLAG_WRPERR          0x00000010U

#elif (PID_HAL_STUB_FAMILY == 4)
/* ---- F4/F3：扇区擦除 + 字编程（注意 F4 也定义了 HALFWORD，所以不能用
 *      "是否定义 HALFWORD" 选分支，否则 F4 会误入 F1 分支） ---- */
#define FLASH_TYPEPROGRAM_BYTE     0x00U
#define FLASH_TYPEPROGRAM_HALFWORD 0x01U
#define FLASH_TYPEPROGRAM_WORD     0x02U
#define FLASH_TYPEPROGRAM_DOUBLEWORD 0x03U
#define FLASH_TYPEERASE_SECTORS    0x00U
#define FLASH_TYPEERASE_MASSERASE  0x01U
#define FLASH_VOLTAGE_RANGE_1      0x00U
#define FLASH_VOLTAGE_RANGE_2      0x01U
#define FLASH_VOLTAGE_RANGE_3      0x02U
#define FLASH_VOLTAGE_RANGE_4      0x03U
#define FLASH_SECTOR_0             0U
#define FLASH_SECTOR_7             7U
#define FLASH_SECTOR_11            11U
#define FLASH_BANK_1               1U
#define FLASH_FLAG_EOP             0x00000001U
#define FLASH_FLAG_OPERR           0x00000002U
#define FLASH_FLAG_WRPERR          0x00000010U
#define FLASH_FLAG_PGAERR          0x00000020U
#define FLASH_FLAG_PGPERR          0x00000040U
#define FLASH_FLAG_PGSERR          0x00000080U

#else
/* ---- H7：扇区擦除 + 32 字节 flashword 编程 ---- */
#define FLASH_TYPEPROGRAM_FLASHWORD 0x04U
#define FLASH_TYPEERASE_SECTORS     0x00U
#define FLASH_TYPEERASE_MASSERASE   0x01U
#define FLASH_SECTOR_0              0U
#define FLASH_SECTOR_7              7U
#define FLASH_BANK_1                1U
#define FLASH_BANK_2                2U
#define FLASH_FLAG_EOP              0x00000001U
#define FLASH_FLAG_ALL_ERRORS       0x0000FEF2U

#endif

#endif /* STM32_HAL_STUB_H */
