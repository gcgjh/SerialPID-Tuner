"""SerialPID-Tuner PC 侧包。

``__version__`` 是版本的**唯一真相**：Windows 版本资源（``scripts/version_info.txt``）、
``docs/CHANGELOG.md`` 的最高版本、``README.md`` 的当前版本行、以及打包产物文件名
（``dist/SerialPID-Tuner-v<版本>.exe``）全部由它派生。

改版本只改这里一处，然后执行 ``python tools/make_version_info.py`` 重新生成版本资源；
``pc/tests/test_version_consistency.py`` 会检查其余各处是否跟着对齐。
"""

__version__ = "0.2.0"
