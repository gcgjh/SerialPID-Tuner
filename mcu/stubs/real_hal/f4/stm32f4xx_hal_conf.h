/**
 * @file    stm32f4xx_hal_conf.h
 * @brief   交叉验证专用最小 HAL 配置（F4）——**只用于 scripts/verify_mcu_build.ps1**
 *
 * 为什么需要它：真实 HAL 的每个模块头文件都要求存在 stm32f4xx_hal_conf.h，
 * 而这个文件本来由 CubeMX 生成。这里给一份"只开示例用到的模块"的最小版本，
 * 让示例能对着**真实 HAL 头文件**编译一遍，用来核对桩里的签名没有漂移。
 *
 * 只开：RCC / GPIO / DMA / CORTEX / FLASH / UART / TIM（示例实际用到的）。
 * 不要把这个文件当成模板抄进产品工程——产品工程该用 CubeMX 生成的那一份。
 */

#ifndef STM32F4xx_HAL_CONF_H
#define STM32F4xx_HAL_CONF_H

#define HAL_MODULE_ENABLED
#define HAL_RCC_MODULE_ENABLED
#define HAL_GPIO_MODULE_ENABLED
#define HAL_DMA_MODULE_ENABLED
#define HAL_CORTEX_MODULE_ENABLED
#define HAL_FLASH_MODULE_ENABLED
#define HAL_UART_MODULE_ENABLED
#define HAL_TIM_MODULE_ENABLED

#define HSE_VALUE               8000000U
#define HSE_STARTUP_TIMEOUT     100U
#define HSI_VALUE               16000000U
#define LSE_VALUE               32768U
#define LSE_STARTUP_TIMEOUT     5000U
#define VDD_VALUE               3300U
#define TICK_INT_PRIORITY       0x0FU
#define USE_RTOS                0U
#define PREFETCH_ENABLE         1U
#define INSTRUCTION_CACHE_ENABLE 1U
#define DATA_CACHE_ENABLE       1U
#define USE_SPI_CRC             0U

#include "stm32f4xx_hal_rcc.h"
#include "stm32f4xx_hal_gpio.h"
#include "stm32f4xx_hal_dma.h"
#include "stm32f4xx_hal_cortex.h"
#include "stm32f4xx_hal_flash.h"
#include "stm32f4xx_hal_uart.h"
#include "stm32f4xx_hal_tim.h"

#endif /* STM32F4xx_HAL_CONF_H */
