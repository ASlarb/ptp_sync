# PTP 同步：硬件时间戳后端与软件回退

本目录提供两种明确分离的运行方式：

- **硬件后端（生产用途）**：Python 只负责探测、配置和监控；Linux 使用
  `ptp4l` / `phc2sys`，Windows 使用 W32Time `PtpClient`。时间戳和时钟
  servo 都由操作系统 PTP 栈及网卡 PHC 完成。
- **软件回退（实验用途）**：用 Python/UDP 交换 Sync / Follow_Up /
  Delay_Req / Delay_Resp，并按下式估算偏差：

```
offset = ((t2 - t1) - (t4 - t3)) / 2    # slave 相对 master 的时钟偏差
delay  = ((t2 - t1) + (t4 - t3)) / 2    # 单向网络时延
```

软件回退建议拓扑：

- **Linux = Grandmaster（主钟）**
- **Windows = Slave（从钟，把本地时钟拉到 Linux）**

角色可以对调：同一套程序两边都能当 master 或 slave。

软件时间戳没有网卡硬件 PTP，局域网典型精度大约 **几十微秒到一两毫秒**，
不能作为硬件 PTP 使用。

Python 编排层只依赖 Python 3.10+ 标准库。

在两端都复制 `ptp_sync/` 目录及项目根目录的 `ptp_sync.py`，并从项目根目录
执行命令：

```bash
python3 ptp_sync.py --help
python3 -m ptp_sync --help
```

硬件模式还依赖操作系统组件和管理员权限，详见下文。所有命令示例中的 `eno1`、
`Ethernet`、IP、domain 和 UTC offset 都必须替换为现场值。

## 快速选择运行模式

如果目标是生产同步或微秒以下精度，使用 `hardware`：

```text
外部 Grandmaster
├── UDP/IPv4 PTPv2 ──> Linux NIC PHC ──phc2sys──> Linux CLOCK_REALTIME
└── UDP/IPv4 PTPv2 ──> Windows NIC/NDIS ──W32Time──> Windows system clock
```

如果只是验证网络连通性、估算两台机器的时钟差，或者网卡/驱动不支持硬件
timestamp，使用 `software`。软件模式不会自动升级为硬件模式，硬件模式失败时
也不会自动降级为软件模式。

建议按以下顺序操作：

1. 确定 Grandmaster、slave、网口、domain 和网络 profile。
2. 在目标机器运行 `hardware detect`，先确认硬件和驱动能力。
3. 运行 `hardware plan` 审核即将发生的系统变更。
4. 在本地控制台执行 `hardware apply ... --yes`。
5. 用 `hardware status` 确认协议锁定和 servo 收敛。
6. 用独立硬件测量最终误差。
7. 需要撤销时执行 `hardware restore`。

## 关键术语

- **Grandmaster（GM）**：PTP 域中的最终时间源。
- **PHC（PTP Hardware Clock）**：网卡内部硬件时钟，Linux 通常暴露为
  `/dev/ptpN`。
- **hardware timestamp**：报文在 NIC 接收/发送位置生成的时间戳，避免
  Python 调度、系统调用和大部分协议栈延迟。
- **two-step**：Sync 报文之后通过 Follow_Up 提供精确发送时间。
- **E2E**：slave 使用 Delay_Req / Delay_Resp 测量端到端路径时延。
- **servo**：持续调整 PHC 或系统时钟频率/相位的控制环。
- **TAI−UTC offset**：PTP timescale 与 UTC 的秒差。该值错误时，设备可能
  “精确地错几十秒”。

## 硬件 PTP 支持范围

- 外部硬件 Grandmaster → Linux slave：支持。
- 外部硬件 Grandmaster → Windows slave：支持。
- Linux `ptp4l` master → Windows slave：支持，但 Linux 系统时钟必须已有
  可追溯 UTC 来源，并正确提供当前 TAI−UTC offset。
- Windows master → Linux slave：**不支持**。Windows 原生 W32Time 只有
  PTP client/subordinate，没有 master 服务。需要外部 GM。

“网卡芯片支持 IEEE 1588”并不等于操作系统可以使用硬件时间戳。固件、驱动、
PHY、VLAN/bonding 和交换机都可能改变有效能力。`hardware detect` 会检查实际
驱动暴露的 TX/RX hardware timestamp、PHC/cross timestamp；条件不满足时会
明确失败，绝不会静默退回软件模式。

生产网络必须使用 IEEE 1588v2 UDP/IPv4、标准端口 319/320、two-step、E2E，
两端 domain 一致。Grandmaster 必须发布有效的 PTP timescale、UTC offset 和
`currentUtcOffsetValid`。

### 部署前检查清单

硬件模式至少需要满足：

- 两端为物理机或能够直通 PHC/硬件 timestamp 的环境；普通虚拟机通常不满足。
- NIC、驱动和固件同时支持 PTPv2 UDP/IPv4 TX/RX hardware timestamp。
- Linux 能看到 `/dev/ptpN`；Windows 驱动能通过 NDIS 暴露
  `*PtpHardwareTimestamp` 和 cross timestamp。
- 交换机允许 UDP 319/320、PTP 组播及返回的单播 Delay_Req/Delay_Resp。
- 所有参与者使用相同 PTP domain、two-step 和 E2E delay mechanism。
- 链路中存在非对称路径时，应使用 PTP transparent/boundary switch；普通交换机
  不能消除排队不对称误差。
- 只有一个进程调整 `CLOCK_REALTIME`。chrony、ntpd、systemd-timesyncd、
  W32Time NTP provider 和其他 PTP daemon 不能同时争用系统时钟。
- 主机防火墙放行标准硬件 PTP 端口 UDP 319/320。
- 在远程机器执行 `apply` 前准备带外控制台，因为重启 Windows 网卡会断开当前
  SSH/RDP/WinRM 会话。

外部 GM 应至少提供以下信息：

- GM IPv4 地址；
- PTP domain；
- 当前 TAI−UTC offset；
- 使用的 profile、two-step/one-step 和 E2E/P2P；
- Sync/Announce 周期；
- 是否经由 boundary/transparent clock。

## Linux 硬件模式

先安装系统组件，例如 Debian/Ubuntu：

```bash
sudo apt install linuxptp ethtool
```

Fedora/RHEL 系可使用：

```bash
sudo dnf install linuxptp ethtool
```

先确认实际网口名称和驱动能力：

```bash
ip -br link
sudo ethtool -T eno1
ls -l /sys/class/net/eno1/ptp
ls -l /dev/ptp*
```

`ethtool -T` 至少应包含：

```text
hardware-transmit
hardware-receive
hardware-raw-clock
PTP Hardware Clock: 0
HWTSTAMP_TX_ON
HWTSTAMP_FILTER_PTP_V2_...
```

只有 capability 文本还不够，最终以 `hardware detect` 的结果为准。bond/team
接口不能直接作为 `phc2sys -s` 的硬件时钟源，应使用能映射到 PHC 的物理接口，
或者按 linuxptp 文档设计 boundary clock。

探测和预览不会修改系统：

```bash
sudo python3 ptp_sync.py hardware detect --interface eno1
sudo python3 ptp_sync.py hardware plan --interface eno1 --role slave
sudo python3 ptp_sync.py hardware detect --interface eno1 --json
```

`detect` 会检查：

- 当前平台和 root 权限；
- `ptp4l`、`phc2sys`、`pmc`、`ethtool`、`systemctl`、`journalctl`；
- NIC 的 TX/RX hardware timestamp、PHC index 和 PTPv2 RX filter；
- UDP 319/320 是否已经被其他进程占用；
- chrony、ntpd、systemd-timesyncd 等冲突服务；
- master 模式下系统时钟是否已有同步源以及是否给出 UTC offset。

退出码为 `0` 表示所有必需检查通过；退出码 `2` 表示至少一项前置条件不满足。
`plan` 会同时列出将写入的配置、将停止的服务以及生成的 systemd 内容。

确认计划后启用。该操作会停止冲突的 NTP/time service，安装本工具专属
systemd 单元，并让 `phc2sys` 接管 `CLOCK_REALTIME`：

```bash
sudo python3 ptp_sync.py hardware apply --interface eno1 --role slave --yes
sudo python3 ptp_sync.py hardware status --interface eno1 --role slave
```

slave 模式的数据流为：

```text
外部 GM -> ptp4l -> NIC PHC -> phc2sys -> CLOCK_REALTIME
```

`apply` 成功必须同时满足：

- `ptp-sync-ptp4l.service` 与 `ptp-sync-phc2sys.service` 为 active；
- ptp4l 端口进入 `SLAVE`；
- `offsetFromMaster` 收敛到 1 ms 内；
- phc2sys 日志进入 `s2` servo 状态且 PHC/系统时钟偏差收敛到 1 ms 内。

查看原始系统状态：

```bash
systemctl status ptp-sync-ptp4l.service ptp-sync-phc2sys.service
journalctl -u ptp-sync-ptp4l.service -f
journalctl -u ptp-sync-phc2sys.service -f
sudo pmc -u -b 0 -d 0 -s /run/ptp-sync/ptp4l.sock \
    "GET CURRENT_DATA_SET"
sudo pmc -u -b 0 -d 0 -s /run/ptp-sync/ptp4l.sock \
    "GET PORT_DATA_SET"
```

恢复启用前的服务、配置和硬件时间戳策略：

```bash
sudo python3 ptp_sync.py hardware restore --interface eno1
```

Linux 作为 master 时必须显式提供经过核实的当前 TAI−UTC offset。下面的 37
只适用于当前 leap-second 状态，不能永久硬编码：

```bash
sudo python3 ptp_sync.py hardware plan \
    --interface eno1 --role master --utc-offset 37
sudo python3 ptp_sync.py hardware apply \
    --interface eno1 --role master --utc-offset 37 --yes
```

工具只有在系统时钟已同步且可追溯时才允许 master 模式。否则它只是一个时间
不准确的 master，不能称为可信 Grandmaster。

master 模式的数据流与 slave 相反：

```text
可追溯 UTC source -> CLOCK_REALTIME -> phc2sys -> NIC PHC -> ptp4l -> clients
```

master 模式不会停止维持 `CLOCK_REALTIME` 的 NTP/chrony 来源，并强制 ptp4l
保持 server/master 角色。工具会向 clients 发布有效的 UTC offset 和 PTP
timescale 属性。不要仅凭操作系统时间显示正常就假设 UTC offset 正确；应由
GM 管理员依据 leap-second 数据确认。

## Windows 硬件模式

要求 Windows 11 / Windows Server 2022 或更新版本、管理员 PowerShell，以及
驱动实际暴露 NDIS `*PtpHardwareTimestamp` 和硬件 cross timestamp。Windows
仅支持 PTP client：

先在“管理员 PowerShell”确认系统、网卡和 provider：

```powershell
Get-CimInstance Win32_OperatingSystem |
    Select-Object Caption, Version, BuildNumber
Get-NetAdapter -Physical
Get-NetAdapterAdvancedProperty -Name "Ethernet" -AllProperties |
    Where-Object RegistryKeyword -eq "*PtpHardwareTimestamp"
Get-Item "$env:SystemRoot\System32\ptpprov.dll"
```

如果驱动完全没有暴露 `*PtpHardwareTimestamp`，不要仅靠手工创建一个同名
注册表值来绕过检查；这不能让驱动产生硬件时间戳。应安装 NIC 厂商提供的正确
驱动/固件，或者更换支持 NDIS packet timestamping 的网卡。

从项目根目录执行：

```powershell
python ptp_sync.py hardware detect --backend windows-ptp `
    --interface "Ethernet" --master 192.168.1.10 --domain 0
python ptp_sync.py hardware plan --backend windows-ptp `
    --interface "Ethernet" --master 192.168.1.10 --domain 0
python ptp_sync.py hardware apply --backend windows-ptp `
    --interface "Ethernet" --master 192.168.1.10 --domain 0 --yes
python ptp_sync.py hardware status --backend windows-ptp `
    --interface "Ethernet" --master 192.168.1.10 --domain 0
```

多个允许的 GM 可以重复传入：

```powershell
python ptp_sync.py hardware plan --backend windows-ptp `
    --interface "Ethernet" `
    --master 192.168.1.10 --master 192.168.1.11
```

Windows `detect` 除了检查高级属性，还会调用 IP Helper timestamp API，分别
检查网卡“支持的能力”和“当前启用的能力”，要求：

- PTPv2 over UDP/IPv4 event/all-message RX hardware timestamp；
- PTPv2 over UDP/IPv4 event/all-message 或 tagged TX hardware timestamp；
- hardware/system cross timestamp；
- `ptpprov.dll`、管理员权限和受支持的 Windows build；
- 合法的 GM IPv4 allowlist；
- 非零 domain 只在当前系统确认支持扩展 PTP 选项时启用。

`apply` 会备份相关注册表值、W32Time 状态、网卡时间戳设置和本工具防火墙规则，
然后重启网卡和 W32Time。远程执行时网卡重启会中断连接。恢复命令：

```powershell
python ptp_sync.py hardware restore --backend windows-ptp
```

应用后可以用系统命令交叉检查：

```powershell
w32tm /query /source
w32tm /query /status /verbose
w32tm /query /configuration /verbose
Get-WinEvent -LogName `
    "Microsoft-Windows-Time-Service-PTP-Provider/PTP-Operational" `
    -MaxEvents 20
```

`hardware status` 只有在以下条件同时满足时才返回 `healthy=True`：

- W32Time 正在运行；
- source 指向允许的 GM；
- PTP provider Reference ID 为 `0x4D505450`；
- PTP Operational 日志存在 master 选择事件；
- NIC 的 active capability 确认正在使用 IPv4 RX/TX hardware timestamp 和
  cross timestamp；
- 请求的 domain 与 provider 配置一致；
- W32Time 状态没有报告未同步。

Windows 域成员还可能受组策略和 Kerberos 时间要求约束；检测到无法安全接管时，
应由域管理员先决定时间源策略。

## 硬件 CLI 完整说明

命令格式：

```text
python ptp_sync.py hardware ACTION [OPTIONS]
```

`ACTION`：

- `detect`：只读探测平台、权限、依赖、NIC/驱动和冲突。
- `plan`：执行探测并输出计划写入的配置和破坏性操作，不修改系统。
- `apply`：保存事务快照、应用配置、等待锁定；失败时自动回滚。
- `status`：检查服务、PTP 端口状态、provider、hardware timestamp 和 servo。
- `restore`：根据事务快照恢复 `apply` 前状态，只撤销本工具拥有的变更。

通用参数：

- `--backend auto|linuxptp|windows-ptp`：默认按当前操作系统自动选择。
- `--interface NAME`：Linux 网口或 Windows adapter 名称。
- `--role slave|master`：Windows 只允许 `slave`。
- `--master IPv4`：Windows GM allowlist，可重复指定。
- `--domain N`：PTP domain，范围 `0..127`。
- `--utc-offset N`：Linux master 的当前 TAI−UTC offset。
- `--state-file PATH`：覆盖事务快照位置，适合测试或受控部署。
- `--json`：输出机器可读 JSON，便于自动化和验收脚本处理。
- `--yes`：仅 `apply` 使用，确认服务切换、系统时钟接管和网卡重启。

硬件命令退出码：

- `0`：操作成功，或 detect/plan 所有必需条件满足；
- `2`：参数、前置检查、权限、能力或配置/回滚失败；
- `3`：`status` 能运行，但当前未达到健康锁定状态。

### 系统变更与事务恢复

Linux 默认使用：

```text
/etc/ptp-sync/ptp4l.conf
/etc/systemd/system/ptp-sync-ptp4l.service
/etc/systemd/system/ptp-sync-phc2sys.service
/run/ptp-sync/ptp4l.sock
/var/lib/ptp-sync/linuxptp-state.json
```

Windows 默认事务文件：

```text
%ProgramData%\ptp-sync\windows-ptp-state.json
```

Linux 快照记录原文件内容和权限、冲突服务状态、已有 managed unit 状态及原
hardware timestamp policy。Windows 快照记录注册表值是否存在及其类型/内容、
W32Time 状态、网卡属性和防火墙规则。`restore` 不使用破坏全部 W32Time
配置的 `w32tm /unregister`。

成功 `apply` 后不要手工删除 state 文件，否则工具无法准确恢复原状态。如果
`apply` 或自动回滚失败，应保留 state 文件和完整错误输出，先修复权限/服务问题，
再执行 `restore`；不要在未知状态下重复 `apply`。

## 软件回退：两台机器上运行

默认端口是 **UDP 31900（event）** 和 **UDP 32000（general）**，普通用户就能绑，不必用特权端口 319/320。

两边都先拷贝整个 `ptp_sync/` 目录（以及旁边的 `ptp_sync.py`）。

**Linux（主钟）**

```bash
python3 ptp_sync.py software master --peer 192.168.1.20
```

`--peer` 填 Windows 的 IPv4。即使不填，Windows 启动后发 Delay_Req，Linux 也会自动登记。

**Windows（从钟，管理员 PowerShell）**

先只观察偏差，不改系统时间：

```powershell
python ptp_sync.py software slave --master 192.168.1.10
```

确认 `offset` 稳定后，再真正改时钟：

```powershell
python ptp_sync.py software slave --master 192.168.1.10 --apply
```

`--apply` 在 Linux 上需要 root，在 Windows 上需要管理员。大偏差（默认 |offset| ≥ 500 µs）会 **step** 时钟；小偏差会 **slew**。

建议始终分两阶段：

1. 不带 `--apply` 连续观察至少几十个样本；
2. 确认 offset、delay 没有明显跳变，且系统没有其他校时服务后再启用
   `--apply`。

每次成功纠时后，slave 会清空纠时前的滤波样本和未完成交换，重新 warmup，避免
旧 offset 被重复应用。slave 还会锁定配置的 master IP 和完整
sourcePortIdentity，其他来源的 Sync/Follow_Up/Delay_Resp 会被忽略。

日志里会看到类似：

```
seq=12 offset=+183.4 us delay=241.0 us filtered=+176.2 us n=12
```

字段含义：

- `seq`：PTP sequence ID；
- `offset`：本次测得的 `slave - master`；
- `delay`：假设上下行对称后的单向路径时延；
- `filtered`：最近窗口的 offset 中位数；
- `n`：进程保存的完成样本数。

`Ctrl+C` 停止时会打印 median / p95 摘要。

软件回退是配套使用的最小 PTPv2 子集，不实现 Announce、BMCA、完整 unicast
协商或安全认证，不能等同于通用 `ptp4l`/W32Time PTP endpoint。UDP 报文本身
没有加密和身份认证，应仅运行在可信、隔离的局域网。软件模式使用标准端口时，
不得与硬件后端同时运行。

## 防火墙

放行两台机器之间的 UDP 31900 和 32000。

Windows 示例：

```powershell
New-NetFirewallRule -DisplayName "Software PTP event"   -Direction Inbound -Protocol UDP -LocalPort 31900 -Action Allow
New-NetFirewallRule -DisplayName "Software PTP general" -Direction Inbound -Protocol UDP -LocalPort 32000 -Action Allow
```

Linux 示例（nft/ufw 按你现有防火墙改）：

```bash
sudo ufw allow 31900/udp
sudo ufw allow 32000/udp
```

## 软件回退常用参数

| 参数 | 含义 |
| --- | --- |
| `--peer IP` | master 主动给这台 slave 发 Sync |
| `--master IP` | slave 侧的 grandmaster 地址 |
| `--apply` | 把测到的 offset 写进系统时钟 |
| `--interval 1.0` | master Sync 周期（秒） |
| `--warmup 4` | `--apply` 前先丢弃的交换次数 |
| `--window 8` | median 滤波窗口 |
| `--step-threshold-us 500` | 超过该偏差则 step，否则 slew |
| `--standard-ports` | 改用标准端口 319/320 |
| `--multicast` | master 的 Sync/Follow_Up 发到 `224.0.1.129`；软件 slave 的 Delay_Req 仍单播到 `--master` |
| `--domain 0` | PTP domain，两边必须一致 |
| `--event-port` / `--general-port` | 本机监听端口 |
| `--peer-event-port` / `--peer-general-port` | 对端监听端口（两台机器用同一组端口时可省略） |
| `--duration 10` | 跑 N 秒后退出 |
| `-v` | 调试日志 |

本机协议自检：

```bash
python3 ptp_sync.py selftest
```

同一台 Linux 上做 loopback 冒烟（端口必须错开，因为 master/slave 不能抢同一个 UDP 端口）：

```bash
python3 ptp_sync.py software master --bind 127.0.0.1 --peer 127.0.0.1 \
    --peer-event-port 31901 --peer-general-port 32001 --duration 6 &
python3 ptp_sync.py software slave --bind 127.0.0.1 --master 127.0.0.1 \
    --event-port 31901 --general-port 32001 \
    --peer-event-port 31900 --peer-general-port 32000 --duration 5
```

两台真实主机用默认 31900/32000 即可，不必设 `--peer-*-port`。

## 软件回退精度说明

- Linux 收包尽量用 `SO_TIMESTAMPNS` 内核时间戳。
- Windows 用 `GetSystemTimePreciseAsFileTime` 读时钟；纠时用 `NtSetSystemTime` / `SetSystemTimeAdjustment`。
- 路径不对称（Wi-Fi、软件中断）会直接变成残余 offset。有线、同交换机、关节能网卡会好很多。
- 不要同时开 NTP / 其他校时服务，否则会和 PTP 抢时钟。

Windows 可先停掉 NTP：

```powershell
w32tm /config /syncfromflags:manual /manualpeerlist:"" /update
Stop-Service w32time
```

Linux 可停 systemd-timesyncd / chrony，避免和 slave `--apply` 冲突：

```bash
sudo systemctl stop systemd-timesyncd.service
```

## 常见问题排查

### `interface` 检查失败

- Linux：用 `ip -br link` 获取准确网口名，不要填写显示名称或 IP 地址。
- Windows：用 `Get-NetAdapter -Physical` 获取 `Name`，名称包含空格时加引号。
- 自动选择只在恰好存在一个合适物理接口时生效；多网卡机器必须显式指定。

### `tool:ptp4l/phc2sys/pmc/ethtool not found`

安装 `linuxptp` 和 `ethtool`，再重新执行 `detect`。工具不会在线安装系统包，
也不会从软件模式静默降级。

### Linux 缺少 `hardware-transmit`、`hardware-receive` 或 PHC

依次检查：

```bash
ethtool -i eno1
ethtool -T eno1
dmesg | grep -i ptp
ls -l /sys/class/net/eno1/ptp /dev/ptp*
```

常见原因是使用了通用驱动、NIC firmware 太旧、USB 网卡不支持、虚拟网卡没有
PHC，或者 bonding/VLAN 隐藏了物理接口能力。

### `standard-ports` 检查失败

已有进程占用了 UDP 319/320。查找并停止冲突实例：

```bash
sudo ss -lunp | grep -E ':(319|320)\b'
systemctl --type=service | grep -E 'ptp|time|chrony|ntp'
```

不要同时启动发行版自带的 `ptp4l.service` 和本工具的
`ptp-sync-ptp4l.service`。

### Linux master 报 `traceable-system-clock` 或 `utc-offset` 失败

先确保系统 UTC 已由可靠来源同步：

```bash
timedatectl status
chronyc tracking
chronyc sources -v
```

然后由时间系统管理员确认当前 TAI−UTC offset，并通过 `--utc-offset` 传入。
不要为了通过检查随意填写数值。

### Linux 服务 active，但 `status` 仍未锁定

重点查看：

```bash
journalctl -u ptp-sync-ptp4l.service -n 100 --no-pager
journalctl -u ptp-sync-phc2sys.service -n 100 --no-pager
```

常见原因包括收不到 Announce/Sync、domain 不一致、GM 使用 one-step/P2P、
交换机过滤组播、Delay_Resp 无法返回、UTC properties 无效，或者链路不对称
导致 offset 不能收敛。

### Windows 缺少 `*PtpHardwareTimestamp`

确认 Windows build、厂商驱动和 NIC firmware。设备管理器显示“支持 PTP”并不
代表 NDIS timestamp API 可用。如果 `hardware detect` 的 IP Helper API 检查
失败，不应继续 `apply`。

### Windows W32Time 运行但 source 不是 GM

执行：

```powershell
w32tm /query /source
w32tm /query /status /verbose
Get-WinEvent -LogName `
    "Microsoft-Windows-Time-Service-PTP-Provider/PTP-Operational" `
    -MaxEvents 50
```

检查 GM allowlist、domain、防火墙、PTP timescale 和
`currentUtcOffsetValid`。如果时间稳定相差整整几十秒，首先检查 TAI−UTC
offset，而不是调节 servo。

### 提示 transaction state 已存在

这表示之前的 `apply` 已成功或未完成。先运行 `hardware status`，确认是否仍在
使用该配置；需要退出时运行 `hardware restore`。不要直接删除 state 文件后
再次执行 `apply`。

### `restore` 失败

保持 state 文件不变，检查管理员/root 权限、systemd/W32Time、网卡和防火墙
管理命令是否可用，然后重试。恢复失败时工具会保留事务信息，不应使用
`w32tm /unregister` 或手工删除所有时间服务配置。

## 硬件在环验收

只有 `detect` 通过不代表同步已经建立。目标机器上还应确认：

1. `hardware status` 返回 `healthy=True` 和 `state=locked`。
2. Linux `pmc` 的端口状态为 `SLAVE`（或受控 master 的 `MASTER`），
   `offsetFromMaster` 连续稳定，`ptp4l` 与 `phc2sys` 均为 active。
3. Windows PTP Operational 日志出现近期 master 选择事件，`w32tm /query
   /source` 指向预期 GM，`w32tm /query /status /verbose` 没有同步错误。
4. 用独立测量方法（示波器/PPS、支持 PTP 的测试仪或硬件 timestamp 抓包）
   验证最终误差。普通 `ping`、应用层时间打印和 Wireshark 软件时间戳不能证明
   已达到硬件级精度。
5. 断开 GM、重启两端并恢复链路，验证 holdover、自动重锁和事务恢复行为。

软件回退和系统硬件 PTP 不得同时绑定 319/320 或同时调整同一系统时钟。

## 项目结构与开发自检

主要文件：

```text
ptp_sync.py                         顶层入口
ptp_sync/cli.py                     software/hardware 主 CLI
ptp_sync/hardware/cli.py            硬件子命令与输出
ptp_sync/hardware/linuxptp.py       Linux linuxptp/systemd 后端
ptp_sync/hardware/windows_ptp.py    Windows NDIS/W32Time 后端
ptp_sync/hardware/state.py          原子事务快照
ptp_sync/master.py                  软件回退 master
ptp_sync/slave.py                   软件回退 slave/filter/servo
ptp_sync/protocol.py                PTPv2 子集编解码
ptp_sync/transport.py               UDP 与 Linux SO_TIMESTAMPNS
ptp_sync/clock.py                   软件回退系统时钟调整
ptp_sync/tests/                     不修改系统服务/时钟的单元测试
```

修改代码后运行：

```bash
python3 -m compileall -q ptp_sync ptp_sync.py
python3 ptp_sync.py selftest
python3 -m unittest discover -s ptp_sync/tests -v
```

单元测试使用 fake runner 验证配置生成、能力解析、事务幂等/恢复和软件 servo，
不会证明实际 NIC 已使用 hardware timestamp。最终仍必须完成前述硬件在环验收。
