# NOTICE

本仓库整理的是 UGREEN AX900 / AICSemi AIC8800D80 USB Wi-Fi 网卡在 Linux 上的安装材料。

## 来源与改动

- 驱动源码来自 AICSemi/BrosTrend AIC8800 DKMS 包，版本号为 `aic8800/1.0.9`。
- 固件来自同一条 AIC8800D80 驱动包线，安装路径为 `/lib/firmware/aic8800D80`。
- 本仓库补充了安装脚本、源码构建脚本、诊断脚本和文档。
- 本仓库的源码树额外补入了 `368b:8d81` 主驱动 USB ID；当前验证过的 UGREEN AX900 设备实际工作态为 `368b:8d85`。

## 已验证环境

- 机器：HP Z420 Workstation
- 系统：Ubuntu 24.04.4 LTS
- 内核：`6.17.0-14-generic`
- 架构：`x86_64`
- 设备：`368b:8d85 AICSemi AIC 8800D80`
- 驱动：`aic8800_fdrv`

## 授权边界

驱动源码文件保留原始版权声明。固件文件是设备运行所需的厂商二进制固件，不是本仓库原创源码。使用、再分发和集成时，请同时遵守各文件自带的版权与许可证声明。

