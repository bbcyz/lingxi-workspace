# 活动包开发规范

活动以独立目录开发和维护。短期活动与长期活动使用同一种格式，分别通过清单中的 `temporary` 设置是否在到期后自动停止。

## 导入、更新和导出

1. 在本机打开「平台管理 → 功能开关」，点击「导入活动包」并选择 ZIP。每个 ZIP 对应一个活动。
2. 列表显示当前版本和待生效版本。退出并重新启动工具台，使新版本注册页面和后台任务。Windows 从托盘退出，Android 使用「停止并退出」。
3. 更新时保留活动 `id`、目录名、页面入口和导航位置，并提高 `version`。活动继续使用原有配置、任务、记录和有效凭据。若新版本无法加载，平台显示失败版本，并尝试加载可用的旧版。
4. 点击活动行的「导出活动包」即可下载可共享的 ZIP。有运行版本时导出当前版本，首次导入尚待生效时导出已保存的版本。ZIP 内容来自活动源码目录。账号和运行状态由平台单独存放。

活动包内的 Python 以工具台进程权限运行，只导入可信来源的包。

## 文件布局

```text
example_campaign.zip
└── example_campaign/
    ├── manifest.json
    ├── __init__.py
    ├── plugin.py
    ├── client.py                  # 视需要添加
    ├── scheduler.py               # 视需要添加
    ├── templates/
    │   └── example_campaign.html
    └── static/
        └── example-campaign.js
```

`manifest.json` 使用 UTF-8。

```json
{
  "format_version": 1,
  "id": "example-campaign",
  "version": "1.0.0",
  "title": "示例活动",
  "module": "activities.example_campaign.plugin",
  "home": "/example-campaign",
  "temporary": true,
  "navigation": "main"
}
```

| 字段 | 约定 |
| --- | --- |
| `format_version` | 平台活动包接口的版本，当前为整数 `1`。 |
| `id` | 全平台唯一。小写字母开头，最多 64 字符，可含小写字母、数字和 `-`。用于配置、凭据和任务的命名空间。 |
| `version` | 活动本身的版本。使用三个以点分隔的整数，例如 `1.2.3`。 |
| `title` | 用户在平台管理和导航中看到的名称，最多 80 字符。 |
| `module` | `activities.<目录名>.plugin`。目录名使用小写字母开头，最多 64 字符，可含小写字母、数字和 `_`。目录内以相对导入引用同包代码。 |
| `home` | `navigation` 为 `main` 时使用 `/<id>`。为 `checkin` 时使用 `/checkin/<id>`。现有微信渠道保留 `/checkin/wechat`。 |
| `temporary` | `true` 为短期活动，`false` 为长期活动。 |
| `navigation` | `main` 显示在主导航。`checkin` 显示在「签到有礼」中。缺省为 `main`。 |

ZIP 内放一个活动目录，最多 512 个条目，压缩后不超过 16 MB，解压后不超过 48 MB。测试、缓存、私有文件和运行数据放在包外。可打包 Python 源码、JSON、文档、网页资源、图片和字体。依赖使用平台已提供的 Python 库。活动的私有实现及页面资源放进活动目录，模板名用活动名作前缀以避免冲突。

## Python 接口

`plugin.py` 暴露 `register(app, credentials, accounts, storage)`。注册期间只创建控制器和挂载蓝图，后台工作由平台统一启动。以下是可直接填入 `plugin.py` 的最小示例，连同上面的清单和一个空的 `__init__.py` 即可导入。

```python
from threading import RLock

from flask import Blueprint, jsonify


class Scheduler:
    def __init__(self):
        self.lock = RLock()

    def start(self):
        pass


class Controller:
    def __init__(self):
        self.deleted = False
        self.scheduler = Scheduler()

    def metadata(self):
        return {'active': True, 'ended': False}

    def busy_login(self, login_id):
        return False

    def forget(self, login_id):
        pass

    def stop(self, wait=True):
        pass


def register(app, credentials, accounts, storage):
    controller = Controller()
    blueprint = Blueprint('example_campaign', __name__)

    @blueprint.before_request
    def check_deleted():
        if controller.deleted:
            return jsonify(error='活动已移除。'), 410

    @blueprint.get('/example-campaign')
    def page():
        return '<h1>示例活动</h1>'

    @blueprint.get('/api/example-campaign/status')
    def status():
        return jsonify(controller.metadata())

    app.register_blueprint(blueprint)
    return controller
```

- 每个包注册一个与目录同名的 Flask 蓝图。页面在 `home` 下，业务 API 在 `/api/<id>/` 下，静态资源在 `/activities/<id>/static/` 下。微信渠道沿用 `/api/checkin-wechat/`。在蓝图上注册路由、错误处理和页面上下文。
- `metadata()` 从内存或本地状态返回 `active`、`ended`，有截止时间时返回带时区的 ISO 8601 `end_at`。平台定期调用它，保持为本地只读操作。短期活动确认结束后停止任务并隐藏入口。
- `scheduler.start()` 启动任务，`stop(wait=True)` 停止派发并等待正在执行的工作。业务开关会给调度器提供 `feature_enabled()`。采用自定义执行入口时，在排队、重试和提交前检查它。
- `credentials(login_id)` 按账号获取共享 APP 凭据，`accounts()` 返回共享账号列表。需要独立登录的渠道使用独立凭据范围。可选的 `scheduler.login_restored(login_id)` 在共享账号登录恢复后收到通知。
- `busy_login(login_id)` 向平台报告正在进行的验证或提交，`forget(login_id)` 清理该账号的活动状态。使用 `storage.load_activity_data(id)` 和 `storage.save_activity_data(id, data)` 存放配置与记录。活动会话可用 `app.extensions['credential_cache']` 按账号和业务范围维护。新包的数据和凭据按活动标识隔离。更新代码时兼容旧记录。
- 业务在 Python 中走受支持的接口。按官方有效期复用同账号、同认证域凭据，保留验证码与提交的会话绑定。待确认的提交先查官方结果再决定是否重试。

## 开发与交付

1. 参考 `activities/daily_gold/` 或 `activities/seven_day/` 开发短期活动，参考 `activities/wechat_checkin/` 开发独立账号渠道。各目录里的 `manifest.json` 是与平台对接的清单。现有内置活动引用了平台随附的公共协议模块，导入目标须提供对应运行库。
2. 在项目根目录执行 `python -m activity_packages <活动目录> <输出文件.zip>`。也可以通过平台管理导出已加载的活动。
3. 在测试数据目录验证首次导入、入口与业务状态、开关、到期、升级保留数据。交付 ZIP、版本号和变更说明，其他开发者可直接使用 ZIP 或按同一目录结构继续维护。
