/**
 * @file    main.h
 * @brief   交叉验证专用的 main.h 垫片（F1）——**只用于 scripts/verify_mcu_build.ps1**
 *
 * 产品工程请用 CubeMX 自己生成的那一份。
 */

#ifndef __MAIN_H
#define __MAIN_H

#include "stm32f1xx_hal.h"

void SystemClock_Config(void);
void MX_GPIO_Init(void);
void MX_USART1_UART_Init(void);
void MX_TIM6_Init(void);
void MX_TIM3_Init(void);
void MX_ADC1_Init(void);
void Error_Handler(void);

#endif /* __MAIN_H */
