idf_component_register(
    SRCS "main.c" "board_esp32.c"
    INCLUDE_DIRS "."
    REQUIRES serialpid_core
             esp_driver_uart
             esp_driver_ledc
             esp_driver_gpio
             esp_adc
             nvs_flash
             esp_timer
             esp_common
)

# 板级代码同样要零告警：移植层的每一句都值得被 -Wall -Wextra 盯着
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
