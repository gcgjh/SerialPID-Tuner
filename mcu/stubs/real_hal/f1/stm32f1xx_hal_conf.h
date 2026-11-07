/**
 * @file    stm32f1xx_hal_conf.h
 * @brief   交叉验证专用最小 HAL 配置（F1）——**只用于 scripts/verify_mcu_build.ps1**
 *
 * 同 f4 目录的说明：只开示例用到的模块（RCC/GPIO/DMA/CORTEX/FLASH/UART/TIM/ADC），
 * 配合 CubeMX 之外的场景做"真实 HAL"编译核对。
 */

#ifndef STM32F1xx_HAL_CONF_H
#define STM32F1xx_HAL_CONF_H

#define HAL_MODULE_ENABLED
#define HAL_RCC_MODULE_ENABLED
#define HAL_GPIO_MODULE_ENABLED
#define HAL_DMA_MODULE_ENABLED
#define HAL_CORTEX_MODULE_ENABLED
#define HAL_FLASH_MODULE_ENABLED
#define HAL_UART_MODULE_ENABLED
#define HAL_TIM_MODULE_ENABLED
#define HAL_ADC_MODULE_ENABLED

#define HSE_VALUE               8000000U
#define HSE_STARTUP_TIMEOUT     100U
#define HSI_VALUE               8000000U
#define LSE_VALUE               32768U
#define LSE_STARTUP_TIMEOUT     5000U
#define VDD_VALUE               3300U
#define TICK_INT_PRIORITY       0x0FU
#define USE_RTOS                0U
#define PREFETCH_ENABLE         1U

#include "stm32f1xx_hal_rcc.h"
#include "stm32f1xx_hal_gpio.h"
#include "stm32f1xx_hal_dma.h"
#include "stm32f1xx_hal_cortex.h"
#include "stm32f1xx_hal_flash.h"
#include "stm32f1xx_hal_uart.h"
#include "stm32f1xx_hal_tim.h"
#include "stm32f1xx_hal_adc.h"

#endif /* STM32F1xx_HAL_CONF_H */
