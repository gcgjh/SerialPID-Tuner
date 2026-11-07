# 只做参数与目标定义；实际编译用 build.ps1（Windows 上不依赖 make）

TARGET  = SerialPID-H750
BUILD   = build

CC      = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
SIZE    = arm-none-eabi-size

CPU     = -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard
DEFS    = -DUSE_HAL_DRIVER -DSTM32H750xx -DPID_DEMO_SIMULATED_PLANT=1

INCLUDES = -ICore/Inc \
           -IDrivers/STM32H7xx_HAL_Driver/Inc \
           -IDrivers/CMSIS/Include \
           -I../Inc

CFLAGS  = $(CPU) $(DEFS) $(INCLUDES) -O2 -Wall -Wextra -std=gnu11 \
          -ffunction-sections -fdata-sections -fno-common
LDFLAGS = $(CPU) -T Core/Startup/STM32H750VBTx_FLASH.ld \
          -Wl,--gc-sections -Wl,-Map=$(BUILD)/$(TARGET).map,--print-memory-usage \
          -specs=nano.specs -specs=nosys.specs

C_SOURCES = Core/Src/board.c Core/Src/stm32h7xx_it.c \
            ../Src/main_example.c ../Src/pid_app.c ../Src/pid_calc.c ../Src/pid_comm.c \
            Drivers/CMSIS/Include/system_stm32h7xx.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_cortex.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_rcc_ex.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_gpio.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_uart_ex.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_tim.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_tim_ex.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_adc.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_adc_ex.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_pwr_ex.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash.c \
            Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_flash_ex.c

ASM_SOURCES = Core/Startup/startup_stm32h750xx.s

OBJECTS = $(addprefix $(BUILD)/,$(notdir $(C_SOURCES:.c=.o)))
OBJECTS += $(addprefix $(BUILD)/,$(notdir $(ASM_SOURCES:.s=.o)))
vpath %.c $(sort $(dir $(C_SOURCES)))
vpath %.s $(sort $(dir $(ASM_SOURCES)))

$(BUILD)/%.o: %.c | $(BUILD)
	$(CC) -c $(CFLAGS) $< -o $@

$(BUILD)/%.o: %.s | $(BUILD)
	$(CC) -c $(CFLAGS) $< -o $@

$(BUILD)/$(TARGET).elf: $(OBJECTS)
	$(CC) $(OBJECTS) $(LDFLAGS) -o $@

$(BUILD):
	mkdir -p $@

all: $(BUILD)/$(TARGET).elf

hex: $(BUILD)/$(TARGET).hex
bin: $(BUILD)/$(TARGET).bin

$(BUILD)/$(TARGET).hex: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(OBJCOPY) -O binary $< $@

clean:
	rm -rf $(BUILD)

.PHONY: all hex bin clean
