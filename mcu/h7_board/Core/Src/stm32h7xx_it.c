/**
 * @file    stm32h7xx_it.c
 * @brief   Cortex-M7 异常与板级外设中断入口
 *
 * 只保留本工程实际用到的两个中断：
 *   SysTick  → HAL 1 ms 时基
 *   USART1   → HAL UART 接收/错误状态机
 * 其余向量在启动文件里弱链接到 Default_Handler（死循环），需要时再补。
 */
#include "main.h"

/* ============================ Cortex-M7 ============================ */

void NMI_Handler(void)
{
    while (1) {
    }
}

void HardFault_Handler(void)
{
    while (1) {
    }
}

void MemManage_Handler(void)
{
    while (1) {
    }
}

void BusFault_Handler(void)
{
    while (1) {
    }
}

void UsageFault_Handler(void)
{
    while (1) {
    }
}

void DebugMon_Handler(void)
{
}

void SysTick_Handler(void)
{
    HAL_IncTick();
}

/* ============================ 外设 ============================ */

void USART1_IRQHandler(void)
{
    HAL_UART_IRQHandler(&huart1);
}
