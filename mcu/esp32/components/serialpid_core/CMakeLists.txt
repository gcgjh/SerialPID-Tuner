# 三层核心模块作为独立组件编入（源码仍在 mcu/Src，不复制、不改写）。
#
# 注意：INCLUDE_DIRS 不接受组件目录外的路径（IDF 会直接报错），
# 所以头文件路径用 target_include_directories 补，PUBLIC 才能传给 main。
idf_component_register(
    SRCS
        "${CMAKE_CURRENT_LIST_DIR}/../../../Src/pid_comm.c"
        "${CMAKE_CURRENT_LIST_DIR}/../../../Src/pid_calc.c"
        "${CMAKE_CURRENT_LIST_DIR}/../../../Src/pid_app.c"
)

target_include_directories(${COMPONENT_LIB} PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/../../../Inc")

# 核心模块的承诺是"纯 C11、零 HAL 依赖"，所以这里按最挑剔的告警档编：
# 只要 xtensa GCC 报一条，就是移植层要解决的问题，而不是把告警关掉。
target_compile_options(${COMPONENT_LIB} PRIVATE
    -Wall -Wextra -Werror=implicit-function-declaration)
