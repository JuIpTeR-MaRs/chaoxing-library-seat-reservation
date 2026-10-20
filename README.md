# 超星图书馆座位预约：Windows 本地版

本目录基于 [bear-zd/ChaoXingReserveSeat](https://github.com/bear-zd/ChaoXingReserveSeat)，增加了桌面界面、区域和空座查询、最长 4 小时的分段预约，以及 Windows 定时任务管理。以下步骤适用于**包含 `desktop_gui.py`、`reserve_segments.py` 和 `schedule_task.ps1` 等新增文件的本地版本**；直接克隆上游仓库不会得到这些新增文件。文末保留上游原始说明供参考，当前界面和定时任务以本节为准。

程序使用学习通账号操作座位预约。预约结果、签到和违约规则以学校的学习通页面为准。不同学校的预约入口可能不同；目前不能保证只凭任意学校的账号密码就能列出其全部区域。

如果需要单 EXE Windows 软件包，先完成下面的首次部署，再运行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build-package.ps1`。脚本会在 `dist` 生成一个 ZIP，内含 `SeatDesk.exe`、定时任务脚本和空白配置；详细用法见 [PACKAGE_README.md](PACKAGE_README.md)。打包产物和本机账号配置不会上传到 Git。

## 1. 首次部署

准备 Windows 电脑、Python、学习通账号和本校任意一个区域的选座链接。当前本地环境使用 Python 3.14.5；Python 需包含 `tkinter`。如果换电脑，请复制**这个本地版本的完整项目目录**，不要直接复制旧电脑的 `.venv`。

**推荐：双击 `deploy.cmd`。**它会在项目目录创建 `.venv`、安装缺少的 Python 依赖、仅在 `config.local.json` 不存在时复制配置模板，然后打开预约界面。重复运行不会覆盖已有账号配置。安装依赖需要联网；部署脚本不会提交座位预约，也不会上传仓库。首次进入界面后仍需填写自己的学习通账号和学校选座链接；定时任务要在“定时预约”页点击“保存定时预约”后才会创建。

如果双击失败，可在 PowerShell 中手动部署并查看错误：

在 PowerShell 中进入项目目录，安装依赖：

```powershell
Set-Location 'C:\Users\30408\ChaoXingReserveSeat'  # 换电脑时改为实际路径
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install requests cryptography numpy opencv-python
if (-not (Test-Path .\config.local.json)) { Copy-Item .\config.json .\config.local.json }
```

`config.local.json` 是本机配置，已被 Git 忽略。它会以明文保存学习通密码，**不要上传或分享**。复制出的 `config.json` 是空白模板；首次打开界面后填写账号、密码和学校选座链接即可继续。

### 从手机学习通获取选座链接

这里需要的是**图书馆座位预约里的“选座”页面链接**，不是课程或学校首页链接。获取时只选本校任意一个阅览区，不需要逐个区域复制。

1. 手机登录学习通，确认已切换或绑定到要预约的学校。
2. 从学校在学习通提供的“图书馆座位预约”入口进入，选择预约日期，直到看到阅览区列表以及每个区域的“选座”按钮。不同学校的入口名称和位置可能不同。
3. 先正常点进任意区域的“选座”页，查看右上角“⋯”或“三条杠”菜单；如果有“复制链接”，直接复制并粘贴到手机备忘录核对。
4. **如果正常页面没有“复制链接”：**返回阅览区列表，暂时同时关闭手机 Wi-Fi 和移动数据，再点该区域的“选座”。页面加载失败后，打开右上角“⋯”或“三条杠”，点“复制链接”。复制完成后恢复网络。这个方法是为了让学习通显示目标页面的地址，不会提交座位预约。
5. 把链接粘贴到手机备忘录或发给自己的电脑，再检查是否包含 `deptIdEnc=`。在程序的“学校选座链接或 `deptIdEnc`”输入框粘贴**完整链接**，然后点“刷新区域”。

链接格式示例：

```text
https://office.chaoxing.com/front/apps/seat/select?deptIdEnc=0123456789abcdef&id=12345&day=2026-10-20
```

检查时主要看这两项：`deptIdEnc=0123456789abcdef` 是学校座位预约入口的标识，界面用它读取区域列表；`id=12345` 是你点开的那个区域的 `roomid`。上面的值仅为格式示例。`day`、`backLevel`、`pageToken` 等参数可能出现或变化；本程序加载区域时只需 `deptIdEnc`，所以也可以只把这 16 位字符填入输入框。

如果复制出的链接**只有 `id=`，没有 `deptIdEnc=`**，它不足以让当前界面读取该校全部区域。请回到座位预约入口，尝试复制包含 `deptIdEnc` 的选座链接；仍找不到时，先不要猜测学校标识。若菜单没有“复制链接”，可查看页面的“分享”菜单是否能复制网页地址，或向学校图书馆询问该座位预约入口。不要把学习通密码、Cookie 或带个人令牌的完整链接公开发布。

启动方式：双击 `launch_gui.cmd`，或在 PowerShell 中运行以下命令。命令行方式可以看到启动错误。

```powershell
.\.venv\Scripts\python.exe .\desktop_gui.py
```

## 2. 账号与区域

1. 在界面顶部填写学习通账号、密码；密码默认遮挡。
2. 在“学校选座链接或 `deptIdEnc`”输入框粘贴完整链接，或填写其中的 16 位 `deptIdEnc`。
3. 点击“保存账号”，再点“刷新区域”。区域下拉框会显示当前账号在该学校接口中可见的区域与 `roomid`。
4. 换学校或账号时，更换对应的选座链接并刷新区域。程序会清空旧账号的区域选择。

保存账号会更新 `config.local.json`；Windows 定时任务也读取该文件。切换账号后，还应在“定时预约”标签页重新确认区域、座位与时段。

## 3. 手动查询与预约

1. 在“手动查询与预约”标签页选择阅览区。
2. 通过“年 / 月 / 日”下拉框选择日期，或点击“今天”“明天”。
3. 通过“时 / 分”下拉框选择开始和结束时间，点击“查询可预约座位”。结果是**整个所选时段**可用的座位。
4. 选中座位，点击“预约选中座位”。程序会重新核对空座，再提交预约；当前手动提交仅支持今天或明天。
5. 查看逐段结果，并到学习通“我的预约”核对记录及签到要求。

单次提交超过 4 小时曾被学校接口拒绝。程序会自动拆分长时段，例如 `11:00–21:00` 拆成 `11:00–15:00`、`15:00–19:00`、`19:00–21:00`。各段独立返回，可能部分成功；查询后座位状态也可能变化。

## 4. 定时预约

1. 先保存账号和学校选座链接，并刷新区域。
2. 打开“定时预约”标签页，选择**执行时间**和执行星期、阅览区、座位号，以及**次日**预约时段。
3. 点击“保存定时预约”。程序会保存 `config.local.json`，并为当前 Windows 用户创建或更新名为 `ChaoXingReserveSeat` 的每周任务。已有任务的启动命令和其他设置会保留。
4. 点击“刷新任务状态”查看下一次执行、上次执行和结果；点击“查看运行日志”查看 `last-run.log`。

所选星期是**执行日**，任务预约执行日的第二天，例如周日 22:00 执行预约周一座位。电脑应在执行时间开机、保持该 Windows 用户已登录，且本机时间与学校预约时间一致。关闭界面不会删除任务。任务显示 `Ready` 只表示已就绪，**不代表预约成功**。

在项目目录的 PowerShell 中可以进行**只读检查**：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1 -CheckOnly
Get-ScheduledTask -TaskName ChaoXingReserveSeat
Get-ScheduledTaskInfo -TaskName ChaoXingReserveSeat | Select-Object LastRunTime,NextRunTime,LastTaskResult
Get-Content .\last-run.log -Encoding UTF8 -Tail 30
```

`-CheckOnly` 只显示房间、座位和时段拆分，**不提交预约**。日志在任务实际运行后生成；文件不存在时先查看 `LastRunTime` 和 `LastTaskResult`。结果代码 `0` 表示任务进程正常结束，仍要核对各段返回及学习通记录；`267014` 表示任务被终止，不能视为预约成功。

暂停或恢复自动任务：

```powershell
Disable-ScheduledTask -TaskName ChaoXingReserveSeat
Enable-ScheduledTask -TaskName ChaoXingReserveSeat
```

## 5. 命令行操作（可选）

以下命令在项目目录中运行，把日期和 `roomid` 换成实际值。

**只查询空座，不提交：**

```powershell
.\.venv\Scripts\python.exe .\query_available_seats.py -u .\config.local.json --date 2026-10-20 --roomid 12345 --start 18:30 --end 21:00
```

在该命令末尾添加 `--reserve-seat 095` 会**真实提交今天**的指定座位；这个 CLI 选项目前只支持今天。

**只检查次日分段配置：**

```powershell
.\.venv\Scripts\python.exe .\reserve_segments.py -u .\config.local.json --check-only
```

**立即真实提交次日预约：**

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run-local.ps1
```

最后一条会提交预约并覆盖 `last-run.log`。`reserve_segments.py` 默认检查配置中的执行星期；`--ignore-weekday` 可跳过星期检查，但也会真实提交。上游 `main.py`/`config.json` 属于原始入口；当前桌面界面和 Windows 任务使用 `config.local.json`、`run-local.ps1` 与 `reserve_segments.py`。

## 6. 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 双击后没有窗口 | 检查 `.venv\Scripts\pythonw.exe`；用 `.venv\Scripts\python.exe .\desktop_gui.py` 看错误。 |
| 区域为空或不是本校 | 核对账号和链接中的 `deptIdEnc`，点击“刷新区域”；不同学校接口可能不兼容。 |
| 无法保存定时预约 | 先刷新当前学校的区域并选择其座位；检查当前 Windows 用户能否创建计划任务。 |
| 查询无空座 | 核对日期、开放时间和完整时段；可换时段重新查询。 |
| 只有部分时段预约成功 | 到学习通核对各段记录，再按剩余空座调整。 |
| 任务 `Ready` 却没预约 | 查看 `Get-ScheduledTaskInfo`、`last-run.log` 和学习通“我的预约”。 |
| 上次结果 `267014` | 任务被终止；检查执行时限、网络、登录过程和日志。 |
| 验证码或接口变化 | 当前程序不保证适配所有学校及验证码类型，需核对学校实际页面和接口返回。 |

不要把 `config.local.json`、含个人预约信息的运行日志或学习通密码提交到 Git。

---

## 上游原始说明（历史参考）

下面是原仓库保留的 `main.py`、`config.json` 和 GitHub Actions 说明，不适用于上述本地 GUI/Windows 定时任务的部署流程。

# ChaoXingServerSeat
超星图书馆座位预约脚本

（由于部分学校新增了点选式行为验证码导致原本的程序会显示验证失败，详细参见issue21[https://github.com/bear-zd/ChaoXingReserveSeat/issues/21]）

## 注意

使用python消除了对js的依赖，请拉取最新版程序运行。

该版本试验性支持滑块验证，目前已经过测试可以使用，如果有滑块验证，请参考下面的**高级设置**部分

## 如何使用

### 本地部署方式

#### 1、安装依赖

运行脚本前先安装一个包

```bash
pip install cryptography
```

如果有滑块验证，则需要额外安装numpy和opencv-python

```bash
pip install numpy, opencv-python
```

#### 2、 获取roomid（图书馆id）和seatid（座位号）

在使用之前需要先在如下获取图书馆对应的id和座位号，下面的配置里已经提供了上海大学图书馆的id。对于不知道id的，可以通过如下方式进行：

![image-20231012153826054](https://zideapicbed.oss-cn-shanghai.aliyuncs.com/img/image-20231012153826054.png)

在进入预约图书馆列表界面时断开网络，点击你想预约的图书馆的`选座`按钮，会提示网页无法打开，此时点击`右上角的三条杠`，选择`复制链接`，会得到类似这样的链接：

> https://office.chaoxing.com/front/apps/seat/select?id=5483&day=2023-10-12&backLevel=2&pageToken=0f46f3acc7be4c60862cb9815870ddfd

其中的`id=5483`的5483即为对应图书馆的id，将其填写到config.json中，座位联网后自己挑即可（详细填写参见后面的setting）

#### 3、running

由于脚本是检测系统时间为7点时进行预约（在main.py 第16行），如果有特殊要求可以修改。通过 `python main.py` 运行脚本, 添加参数 `-u config.json` 来指明配置文件路径

运行`python main.py -m debug`可以立即运行查看配置是否正确。

关于运行的方式，现在提供了多种运行方式：

- Linux环境下：

在Linux下可以使用如下方式添加crontab , 运行：`crontab -e`添加指令 :`0 7 * * * python3 main.py`

- windows环境下：

windows下使用时间任务:

![](https://zideapicbed.oss-cn-shanghai.aliyuncs.com/QQ%E5%9B%BE%E7%89%8720221120213736.png)

### github actions部署方式（目前应该没有问题了）：

  这种方式可以不需要在本地部署环境，只需要把fork该仓库并修改配置文件即可。

1.**fork该仓库**

2.**修改config.json**：这个仿照之前的方式进行修改即可，但是注意，username和password请留空或者随便填以防止泄漏个人账号密码。（具体的需要填写在自己repo的settings中）。时间什么也是需要修改（修改到仓库中）不要忘记。

3.**配置账号密码**：在settings->secrets and variables->Repository secrets 创建两个secret keys。名称分别为USERNAMES，PASSWORDS，填写自己的账号和密码即可。（如果有多个用户，请使用,(英文逗号)隔开，如果密码中有逗号可能会出现问题）。

```
xxxxxxx,xxxxxxx
```

4.**运行action**：在action -> auto_reserve -> run workflows 选择main分支即可。


## config配置
之后编辑config.json并填写座位预约相关信息即可
```json
{
    "reserve": [
        {"username": "XXXXXXXX", //https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid=&  在这个网站查看是否可以顺利登陆 
        "password": "XXXXXXXX",
        "time": ["08:00","22:00"], // 预约的起始时间
        "roomid":"2609", //2609:四楼外圈,5483:四楼内圈,2610:五楼外圈,5484:五楼内圈
        "seatid":"002", // 注意要用0补全至3位数，例如6号座位应该填006
        "daysofweek": ["Monday" , "Tuesday", "Wednesday", "Thursday", "Friday"]
        },
        {"username": "xxxxxxxxxx",
        "password": "xxxxxxxxx",
        "time": ["20:00","21:00"],
        "roomid":"5483",
        "seatid":["056"],
        "daysofweek": ["Saturday" , "Sunday"]
    }
}
```
参考前面的运行方式即可。


## 高级设置

在main.py中有四个参数可以选择

```python
SLEEPTIME = 0.2 # 每次抢座的间隔
ENDTIME = "07:01:00" # 根据学校的开始预约座位时间+1min即可

ENABLE_SLIDER = False # 是否有滑块验证，设置为True开启滑块验证
MAX_ATTEMPT = 4 # 最大尝试次数
```
可以直接进行修改，但是不建议把**SLEEPTIME**设置太小。

## 存在的问题

目前日志输出不是很人性化，如果出现了以下问题请提issue：

- 出现了代码逻辑的错误
- {当前人数过多，请等待5分钟后尝试}。这种是请求方式错误或者请求键值错误导致的，通常是由于学习通更新了预约导致的
- 以字典格式输出的其他错误，仔细查看用户名密码，roomid和seatid是否填写正确。如果问题不能解决请在github上提issue
- 滑块验证目前无法进行测试

### 无法预约情况debug方式
> 1、电脑端访问："https://passport2.chaoxing.com/mlogin?loginType=1&newversion=true&fid=" 使用自己的用户名密码登录
> 2、电脑端访问：”https://office.chaoxing.com/front/third/apps/seat/code?id={图书馆id}&seatNum={座位id}“查看是否显示时间表
> 3、尝试预约看看是否会出现验证方式

目前无法实现跨单位座位预约。

