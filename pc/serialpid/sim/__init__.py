"""内置仿真包。

模块分工（卡27 起收敛为一套实现）：

* :mod:`serialpid.sim.params` —— 参数口径（范围/默认/映射），面板、持久化、扫参共用；
* :mod:`serialpid.sim.presets` —— 8 个场景预设与"对应什么真实对象"的说明；
* :mod:`serialpid.sim.plant` —— 被控对象（二阶惯性 + 纯延迟 + 饱和 + 噪声/量化）；
* :mod:`serialpid.sim.transport` —— 进程内通道；
* :mod:`serialpid.sim.engine` —— **唯一的** MCU 替身（命令分发/PID/阶跃/手动步进），
  ``tools/fake_mcu.py`` 只是它的 TCP 薄壳；
* :mod:`serialpid.sim.degradation` —— 链路劣化层（丢帧/坏 CRC/延迟/断线/噪声）；
* :mod:`serialpid.sim.recorder` —— 帧流录制与回放；
* :mod:`serialpid.sim.frames` —— 帧切割/坏 CRC 等小工具；
* :mod:`serialpid.sim.sim_config` —— 仿真参数持久化（``sim_state.json``）。

本文件故意不 import 任何子模块：各子模块按卡并行建立，包初始化不应成为
模块缺失时的导入障碍。用到什么直接 ``from serialpid.sim.<module> import ...``。
"""
