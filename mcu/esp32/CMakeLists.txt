cmake_minimum_required(VERSION 3.16)

# SerialPID-Tuner ESP32 固件（card45）
#
# 三层核心（pid_comm / pid_calc / pid_app）在 ../../../Src 与 ../../../Inc，
# 以组件 serialpid_core 的形式编进来，**一行不改**；ESP32 只提供板级六回调。
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(serialpid_tuner_fw)
