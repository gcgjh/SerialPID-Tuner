/**
 * @file    main.h
 * @brief   交叉验证专用的 main.h 垫片（F4）——**只用于 scripts/verify_mcu_build.ps1**
 *
 * CubeMX 生成的 main.h 本质上就是"包含芯片 HAL 头 + 声明 MX_* 初始化函数"。
 * 这里给一份最小版本，让示例在真实 HAL 头文件下也能编译，从而核对
 * mcu/stubs/stm32_hal_stub.h 里的签名是否与真实 HAL 一致。
 *
 * 产品工程请用 CubeMX 自己生成的那一份，不要用这个。
 */

#ifndef __MAIN_H
#define __MAIN_H

#include "stm32f4xx_hal.h"

void SystemClock_Config(void);
void MX_GPIO_Init(void);
void MX_USART1_UART_Init(void);
void MX_TIM6_Init(void);
void MX_TIM3_Init(void);
void Error_Handler(void);

#endif /* __MAIN_H */
