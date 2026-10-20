# ugreen-ax900

UGREEN AX900 / AICSemi AIC8800D80 USB Wi-Fi 网卡的 Linux 驱动整理仓库。

工程标准入口：`harness/docs/workspace/standards/linux-driver/linux-driver-golden-path.md`。

## 架构拓扑

```mermaid
flowchart LR
    prebuilt["release_assets/ · 预编译驱动包"]
    source["src/aic8800-1.0.9/ · DKMS 驱动源码"]
    install["scripts/install_prebuilt.sh"]
    build["scripts/build_from_source.sh"]
    modules["aic_load_fw + aic8800_fdrv · Linux 内核模块"]
    firmware["firmware/aic8800D80/ · 厂商固件"]
    usb["AX900 USB 网卡 · 存储或固件加载模式"]
    udev["udev/aic.rules · 模式切换规则"]
    wifi["Linux Wi-Fi 接口"]
    diag["scripts/collect_diagnostics.sh"]

    prebuilt -->|"校验 kernel 与 arch 后安装"| install
    source -->|"经 DKMS 构建并安装"| build
    install -->|"部署固定版本模块"| modules
    build -->|"生成当前内核模块"| modules
    firmware -->|"提供设备固件"| modules
    usb -->|"枚举事件触发规则"| udev
    udev -->|"执行 USB 模式切换"| usb
    modules -->|"加载固件并绑定设备"| usb
    usb -->|"重新枚举后创建"| wifi
    diag -->|"只读验证 USB、模块与网络状态"| wifi
```

本仓库提供两种安装方式：

- 直接安装 release 中的预编译驱动包：只适配已验证的 Ubuntu 24.04 / `6.17.0-14-generic` / `x86_64`。
- 从源码通过 DKMS 构建：适合其他内核版本，但需要本机有内核头文件和编译工具链。

## 已验证硬件与系统

历史 `v0.1.0` release 的预编译模块来自下面这台机器。新的分发版本保留相同模块字节；本地重打包与安装状态机测试不能替代目标机硬件验收：

```text
OS      Ubuntu 24.04.4 LTS
Kernel  6.17.0-14-generic
Arch    x86_64
Device  368b:8d85 AICSemi AIC 8800D80
Driver  aic8800_fdrv
```

正常工作时可看到：

```bash
lsusb | grep -Ei '368b|aic'
# Bus 002 Device 005: ID 368b:8d85 AICSemi AIC 8800D80

lsusb -t
# Driver=aic8800_fdrv

nmcli device status
# wlx... wifi disconnected/connected
```

## 方案一：安装预编译 release

这个方案最快，但有硬性边界：

- 只允许安装到 `6.17.0-14-generic`。
- 只允许安装到 `x86_64`。
- Secure Boot 开启时不建议使用预编译包；请改用源码构建并按本机策略签名模块。

正式 `1.0.0` 包由下文入口生成。取得对应构建号的包后，先核对发布收据中的归档摘要，再执行：

```bash
tar -xzf "<实际取得的正式 tar.gz 路径>"
cd ugreen-ax900-prebuilt
python3 scripts/verify_package.py verify .
sudo bash scripts/install_prebuilt.sh
```

安装器依赖 Python 3 与 `util-linux` 的 `flock`，拒绝本地验证包、错误内核／架构及校验失败的文件。`/var/lib/ugreen-ax900/install.json` 在安装锁内记录包身份、文件摘要和待完成／已完成状态；较旧版本、较旧构建号或同号不同内容不能覆盖已安装包。中断后使用同一包重试；已有驱动没有记录或记录与实际文件不同，必须先核定历史版本和构建号，不会自动采用现状。

安装完成后，物理拔下网卡再插回，优先使用主板后置 USB 口。然后执行：

```bash
bash scripts/collect_diagnostics.sh
```

预期结果：

- `lsusb` 从初始 `a69c:5723` 或 `a69c:8d80` 变为 `368b:8d85`。
- `lsusb -t` 显示 `Driver=aic8800_fdrv`。
- `ip -br link` 或 `nmcli device status` 出现 `wlx...` Wi-Fi 接口。

## 方案二：从源码构建

适用于当前内核不是 `6.17.0-14-generic` 的情况。

先安装依赖：

```bash
sudo apt update
sudo apt install -y dkms build-essential linux-headers-$(uname -r) usb-modeswitch
```

然后构建并安装：

```bash
git clone https://github.com/darren-you/ugreen-ax900.git
cd ugreen-ax900
sudo bash scripts/build_from_source.sh
```

构建脚本会：

- 把 `src/aic8800-1.0.9` 安装到 `/usr/src/aic8800-1.0.9`。
- 通过 DKMS 构建并安装 `aic_load_fw` 和 `aic8800_fdrv`。
- 安装 `firmware/aic8800D80` 到 `/lib/firmware/aic8800D80`。
- 安装 udev 规则和开机模块加载配置。

构建完成后，同样需要物理拔插一次网卡，再执行：

```bash
bash scripts/collect_diagnostics.sh
```

## 常见问题

### 1. 插上后只看到 `a69c:5723 Aic MSC`

这是网卡的虚拟光盘/大容量存储模式，还不是 Wi-Fi 模式。

处理：

```bash
sudo apt install -y usb-modeswitch
sudo usb_modeswitch -KQ -v a69c -p 5723
```

然后拔插网卡。

### 2. 看到 `a69c:8d80`，但没有 `wlan` / `wlx...`

这是固件加载阶段。正常链路应继续重新枚举成 `368b:8d85` 或相近的 `368b:8d8*` 设备，再由 `aic8800_fdrv` 接管。

处理：

```bash
sudo modprobe aic_load_fw
sudo modprobe aic8800_fdrv
```

随后物理拔插网卡。Z420 上验证过，软件 reset、USB 端口 unbind/bind、EHCI 控制器 reset 都不如物理拔插可靠。

### 3. `dkms build` 失败

先检查内核头文件：

```bash
uname -r
ls -ld /lib/modules/$(uname -r)/build
```

如果缺失：

```bash
sudo apt install -y linux-headers-$(uname -r)
```

如果 `apt` 被本地代理或 fake-ip 影响，先修复系统网络解析，不要把不完整的包状态继续扩大。

### 4. Secure Boot 开启

预编译包不会使用你的本机 MOK 签名。Secure Boot 开启时，请使用源码构建，并按本机安全策略签名内核模块或关闭 Secure Boot。

### 5. `ethtool -i wlx...` 显示 `driver: usb`

这个驱动的 `ethtool` 信息不太可靠。以这两个结果为准：

```bash
lsusb -t
lsmod | grep aic
```

只要看到 `Driver=aic8800_fdrv`，并且 `aic8800_fdrv`、`aic_load_fw` 已加载，就说明驱动绑定正确。

### 6. 升级内核后 Wi-Fi 消失

预编译包只适配固定内核。升级内核后执行源码构建：

```bash
sudo bash scripts/build_from_source.sh
```

## 仓库结构

```text
src/aic8800-1.0.9/       AIC8800 DKMS 源码
firmware/aic8800D80/     AIC8800D80 固件
udev/aic.rules           存储模式自动 eject 规则
scripts/                 安装、构建和诊断脚本
release.json             自建分发版本的唯一声明
prebuilt-source.json     历史原件的来源、摘要、内核和架构
```

## 构建分发包

`release.json` 声明自建分发版本 `1.0.0`。厂商源码 `aic8800/1.0.9` 以及实际模块 `aic_load_fw 1.0`、`aic8800_fdrv 6.4.3.0` 保留原义；它们不是分发构建号。

先将 [历史 v0.1.0 发布原件](https://github.com/darren-you/ugreen-ax900/releases/tag/v0.1.0) 下载到被忽略的 `release_assets/`，文件名与 `prebuilt-source.json` 一致。构建入口会核对声明中的 SHA256，解读实际压缩 ELF 的架构、模块名和 vermagic，只复用原件中的模块、固件与安装配置，使用当前安装脚本及许可文件封装。

```bash
bash scripts/package_release.sh
```

无正式上下文时生成 `dist/*-1.0.0-local.tar.gz` 和 JSON 收据，包内显式标记 `local_validation`，不分配构建号且不能安装。正式构建由已有 Core 分配器提供完整冻结上下文和 `FAST_DEPLOY_CLIENT_VERSION_SNAPSHOT_FILE`，并显式设置 `FAST_DEPLOY_PACKAGE_CORE_ROOT` 指向冻结、干净的 Core。版本必须等于冻结数字分支，架构为 `x86_64`，变体为 `ubuntu24.04-kernel6.17.0-14-generic`；分发身份为 `module / ugreen-ax900`。本脚本消费既有编号，不初始化生产状态或自行核定高水位。

正式输出 `dist/*-<版本>-build<构建号>.tar.gz`，包内 `VERSION`、`build.json`、`native.json` 与 `SHA256SUMS` 一并生成并回读；同一输入重放字节不变，同号异内容拒绝覆盖，历史归档不删除。构建和回归需 Python 3、`zstd`、Git。测试仅在临时目录运行，正式路径测试需显式提供 Core 源码：

```bash
FAST_DEPLOY_TEST_PACKAGE_CORE_ROOT="<实际 Core 绝对路径>" python3 scripts/test_build_package.py
```

源码 DKMS 入口仍是目标机的维护操作，使用厂商版本；它不会发布上述带身份的分发包，也不能作为新分发包的构建或安装证据。

## 许可证与来源

发布包由 `scripts/package_release.sh` 生成；交付前核对生成的 `VERSION`、`build.json`、`native.json`、文件清单、校验值、许可证与安装脚本。安装前后用 `scripts/collect_diagnostics.sh` 对照目标系统、架构、内核、Secure Boot、模块加载和网卡绑定；DKMS 路径还需在目标机验证重启加载与内核升级后的重建。

驱动源码来自 AICSemi/BrosTrend AIC8800 驱动包，保留原始版权和许可证声明。本仓库新增脚本与文档按 `LICENSE` 中的说明发布。固件是厂商二进制固件，见 `NOTICE.md`。
