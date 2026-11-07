/**
 * @file    main.h
 * @brief   STM32H750VBT6 板级定义（HAL 工程入口头文件）
 *
 * 目标板：STM32H750VBT6（H750VB，128 KB 内部 Flash）
 * 时钟  ：HSE 25 MHz → PLL1 400 MHz SYSCLK / 200 MHz HCLK
 * 串口  ：USART1 PA9(TX) / PA10(RX)，115200 8N1
 * 执行器：TIM3_CH4 → PB1（板载示例用的 PWM 脚；换线时改 PID_EXAMPLE_PWM_CHANNEL）
 * 传感器：ADC1_IN10 → PC0（板载 ADC 示例脚）
 * 指示  ：LED PA1
 */
#ifndef MAIN_H
#define MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32h7xx_hal.h"
#include <stdint.h>
#include <stdbool.h>

/* 本板 PWM 通道：TIM3_CH4 / PB1。
 * main_example.c 默认 TIM_CHANNEL_1，这里是唯一需要按板子改的接线口径。 */
#define PID_EXAMPLE_PWM_CHANNEL TIM_CHANNEL_4

/* 板级外设句柄（main_example.c 里 extern 引用） */
extern UART_HandleTypeDef huart1;
extern TIM_HandleTypeDef  htim3;
extern ADC_HandleTypeDef  hadc1;

/* CubeMX 风格的初始化函数 */
void SystemClock_Config(void);
void MX_GPIO_Init(void);
void MX_USART1_UART_Init(void);
void MX_TIM3_Init(void);
void MX_ADC1_Init(void);

void Error_Handler(void);

#ifdef __cplusplus
}
#endif

#endif /* MAIN_H */
