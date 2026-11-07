"""内置仿真包。

当前包含：

* :mod:`serialpid.sim.plant` —— 被控对象模型（二阶惯性 + 纯延迟 + 执行器饱和
  + 测量噪声/量化），由引擎每拍驱动。
* 进程内传输与引擎（``transport`` / ``engine``）另卡实现。

本文件故意不 import 任何子模块：各子模块按卡并行建立，包初始化不应成为
模块缺失时的导入障碍。用到什么直接 ``from serialpid.sim.plant import Plant``。
"""
