## 代码规范

### 15.1 文件头大框(八栏, 必须全保留)

每份 `.py` 文件首部固定八栏, 边框用 `# ====` 上下包, 栏间用 `# ----` 分隔. 本项目里栏目内容按下列语义填写:

```python
# ==========================================================================================
# 文件        : extract.py
# 所属目录    : common/
# 文件用途    : 从 HTTP 响应中按规则提取字段, 并把站点文案翻成签到状态
# 架构定位    : 公共基础层(Model 与 Controller 共用), 不依赖任何平台实现
# 核心功能    : JSON 用 json: 点路径; 关键字匹配; HTML 解析由平台代码用 BeautifulSoup 完成
# 职责边界    : 只取值与判定; 不发请求, 不写配置, 不打日志凭据全文
# 运行环境    : Python 3.12; 本地与 ubuntu-latest(Action) 均可运行
# 依赖关系    : 上游 http.py 提供 Response; 下游 <平台>_checkin.py 调用
# ==========================================================================================
```

### 15.2 导入区

每行尾部注明来源与用途, 不写"无意义 import".

```python
import re                                    # 标准库: 正则规则 re: 匹配与捕获
import json                                  # 标准库: JSON 响应解析与点路径取值
from typing import Any, Optional             # 标准库: 类型标注, 明确返回可为 None

from common.logger import get_logger         # 本项目: 记录规则失效等 WARNING, 不抛异常
```

### 15.3 类注释

每个类都必须有类级 docstring; 私有辅助类也至少用一句话说明用途, 不允许出现空 docstring 或只写类名.

结构顺序固定如下:

1. 一句话定位: 这个类是什么、负责什么.
2. 空行.
3. 工作流程: 有序编号, 说明一次典型调用经历哪些步骤.
4. 实例属性清单: 每行 `属性名 (类型): 说明`.
5. 构造参数: 完整的 `:param:` 与 `:type:` 成对列表.
6. `:return:` 与 `:rtype:`.
7. `:raises:`.

本项目适配点:

- 没有多线程, **不写"线程安全"**, 改写运行约束, 例如"单次运行、实例用完即弃""不持有跨进程状态""实例内可变字段只在本进程内使用".
- 类注释里要写明"谁创建、谁释放"的约定, 例如由 controller 按账号创建 session 适配器, 任务结束即丢弃.

类注释与分区标记的关系:

- 先用 `# =========** 类名 =========**` 分区, **紧随其后**就是类 docstring.
- 分区标记只负责跳转定位, 不重复 docstring 内容, 不再写第二行说明; docstring 里也不再重复类名.

`__init__` 可以只写"参数含义与异常见类注释", 但参数表必须在类注释里完整出现一次, 不允许两处都省.

说明: 类注释的正式结构以本文件末尾附录「类注释与编辑器可见性的对应关系」为准, 15.3 为原始出处; 附录在该结构上补充了方法一览与实例属性的可变性标注.

### 15.4 函数注释(PyCharm 风格)

结构固定: 说明段 -> 空行 -> `:param:` 与 `:type:` 逐参数成对出现 -> `:return:` -> `:rtype:` -> `:raises:`.

```python
def extract_value(response: "Response", rule: str, default: Any = None) -> Optional[Any]:
    """按 json: 点路径从响应中取出一个值.

    只支持 json: 规则; 取不到时返回 default 而不抛出异常.
    HTML 字段由平台代码用 BeautifulSoup 解析, 不走本函数.

    :param response: HTTP 响应包装对象, 含 text 与 content_type
    :type response: Response
    :param rule: 提取规则, 形如 json:data.token
    :type rule: str
    :param default: 取不到时的返回值, 默认 None
    :type default: Any
    :return: 取到的值; 任何失败情形都返回 default
    :rtype: Optional[Any]
    :raises ValueError: 仅当 rule 为空字符串时抛出, 视为配置错误
    """
```

### 15.5 逐行尾注

关键行为与**原因**写行尾注释, 不做逐句翻译. 判定标准: 一行代码如果"看起来显然但改错会有后果", 就写一行注释.

```python
text = response.text or ""                   # 部分站点 204 时 text 为 None, 先归一成空串
if not rule:                                 # 空规则是配置错误, 必须显式失败而不是静默返回 None
    raise ValueError("extract rule is empty")
```

### 15.6 类分区标记

```python
# =========** ExtractRule =========**
class ExtractRule:
    ...
```

类名全大写的分区标记只用于"这一个代码块在讲这个类", 例如文件里类与工厂函数混排时; 单类文件也必须写, 保持全局一致.

- 分区标记的下一行必须是类 docstring, 内容分工见 15.3; 标记本身不写职责说明, 避免与 docstring 重复.

### 15.7 标点规则(强制)

- 注释与代码中: 逗号、冒号、分号、括号、引号、句号一律**半角**.
- 中文与英文/数字之间留**一个空格**, 例: `签到成功 3 个账号`, `读取 state.json 失败`.
- 只有字符串内容本身需要时才用全角, 例: `logger.info("签到成功, 收益 +5")` 里的逗号如果站点文案就是这样, 保留全角.
- 代码块里的 YAML/JSON 注释同样遵守以上规则.

### 15.8 样板代码(全项目参照)

样板给的是"一个完整类 + 一个方法": 类注释写定位、工作流程、实例属性与构造参数, `pick` 方法写完整 PyCharm 注释与逐行尾注.

```python
# ==========================================================================================
# 文件        : extract.py
# 所属目录    : common/
# 文件用途    : 从 HTTP 响应中按规则提取字段, 并把站点文案翻成签到状态
# 架构定位    : 公共基础层(Model 与 Controller 共用), 不依赖任何平台实现
# 核心功能    : JSON 用 json: 点路径; 关键字匹配; HTML 解析由平台代码用 BeautifulSoup 完成
# 职责边界    : 只取值与判定; 不发请求, 不写配置, 不打日志凭据全文
# 运行环境    : Python 3.12; 本地与 ubuntu-latest(Action) 均可运行
# 依赖关系    : 上游 http.py 提供统一返回对象; 下游 <平台>_checkin.py 调用
# ==========================================================================================

import json                                  # 标准库: JSON 响应解析与点路径取值
from typing import Any, Optional             # 标准库: 类型标注, 明确返回可为 None

from common.logger import get_logger         # 本项目: 规则失效时记 WARNING, 不抛异常


# =========** 模块级常量 =========**
_LOGGER = get_logger()                       # 无平台上下文时写 logs/checkin.log
_JSON_PREFIX = "json:"                       # 唯一规则前缀: JSON 点路径


# =========** JsonExtractor =========**
class JsonExtractor:
    """按 json: 点路径从统一返回对象中取值, 一次运行内复用.

    工作流程:
        1. 构造时校验规则表非空, 并按名字浅拷贝到实例属性.
        2. pick 按名字取出规则, 得到 json:data.token 这类路径.
        3. 逐段下钻 dict / list; 任一段缺失即返回兜底值.
        4. 解析失败只记 WARNING, 不抛异常, 以免打断批量签到.

    实例属性:
        rules (dict[str, str]): 字段名到 json: 路径的映射, 构造后只读.
        default (Any): 调用方未传 default 时的兜底值.

    运行约束: 单次运行、实例用完即弃; 不持有跨进程状态, 不共享可变全局.

    :param rules: 字段名到 json: 路径的映射, 如 json:data.token
    :type rules: dict[str, str]
    :param default: 未命中时的兜底值, 默认 None
    :type default: Any
    :return: 构造完成的提取器实例
    :rtype: JsonExtractor
    :raises ValueError: rules 为空时抛出, 视为配置错误
    """

    def __init__(self, rules: dict[str, str], default: Any = None) -> None:
        """初始化提取器.

        参数含义与异常见类注释, 这里只做字段落位, 不触发 IO.

        :param rules: 字段名到 json: 路径的映射
        :type rules: dict[str, str]
        :param default: 未命中时的兜底值, 默认 None
        :type default: Any
        :raises ValueError: rules 为空时抛出
        """
        if not rules:                        # 空规则表一定是配置问题, 启动时就该暴露
            raise ValueError("rules is empty")
        self.rules = dict(rules)             # 浅拷贝, 避免调用方后续改动影响本次运行
        self.default = default               # 实例级兜底, 方法级 default 优先

    def pick(self, name: str, response: Any, default: Any = None) -> Optional[Any]:
        """按规则名从 JSON 响应中取出一个值.

        只支持 json: 点路径; 任何解析失败都收敛成兜底值, 不向上抛.

        :param name: 规则名, 必须是 rules 里已有的键
        :type name: str
        :param response: 统一返回对象(dict)或带 text 属性的旧包装对象
        :type response: Any
        :param default: 本次调用的兜底值, 优先于实例级 default
        :type default: Any
        :return: 取到的值; 未命中或解析失败时返回兜底值
        :rtype: Optional[Any]
        :raises KeyError: name 不在 rules 中时抛出, 属于平台代码写错了规则名
        """
        rule = self.rules[name]              # 规则名写错是编码错误, 直接抛 KeyError 而不是静默兜底
        path = rule[len(_JSON_PREFIX):] if rule.startswith(_JSON_PREFIX) else rule

        if isinstance(response, dict):       # 第九章的统一返回结构就是普通 dict
            text = response.get("text") or ""
        else:
            text = getattr(response, "text", "") or ""  # 204 的 text 可能为 None, 先归一成空串

        try:
            node: Any = json.loads(text)     # 站点偶发返回 HTML 错误页, 这里会抛异常
            for part in path.split("."):     # 点路径逐段下钻
                node = node[int(part)] if isinstance(node, list) else node[part]
        except Exception as exc:             # 字段缺失/类型不符/非 JSON 都收敛在此
            _LOGGER.warning("提取失败 name=%s err=%s", name, exc.__class__.__name__)
            return default if default is not None else self.default

        if node is None or node == "":
            return default if default is not None else self.default
        return node
```

说明: 类注释里的工作流程对应一次典型调用(构造 -> 取规则 -> 逐段下钻 -> 命中或兜底); `__init__` 只做参数落位, 不触发 IO, 便于在 `test` 动作里干跑; HTML 解析不在 `extract.py` 内, 由平台代码用 `BeautifulSoup(html, "html.parser")` 按 8.1 的顺序完成; `re:` 只出现在平台代码的时间/数值提取里, 不是 `extract.py` 的规则前缀.

## 待确认项（均已确认）

> 本节不是规范正文的一部分, 仅为历史待确认记录, 正文未作任何改动; 下列条目均已确认, 无待决事项。

- (1) 已确认保留: 15.3 要求在类注释中出现 `:return:` / `:rtype:` / `:raises:`。语义固定为「构造完成的实例」与「构造阶段可能抛出的异常」, 与常见 Python 生态写法不同, 但按本规范执行, 不再列为待确认项。
- (2) 已确认: 15.2 与 15.8 的 import 尾注维持固定列对齐, 不采用「仅一个空格起注」的放宽方案; `ruff format` / `black` 不维护该对齐, 对齐细则见附录「import 尾注对齐规则」。
- (3) 已确认: 书名号《》与直角引号「」在 Markdown 文档中允许使用, 在代码注释与 docstring 中禁用, 改用半角引号; 判据按此执行, 不再列为待确认项。

## 附录：类注释与编辑器可见性的对应关系

> 本节不是规范正文的一部分, 仅为示例附录, 不改变正文任何条款, 也不新增约束。

类注释按固定栏目书写, 才能在鼠标悬停类名时一次看全。定稿结构(正式口径):

1. 一句话定位。
2. 空行。
3. 工作流程: 有序编号, 说明一次典型调用经历哪些步骤。
4. 实例属性: 每行「属性名 (类型): 说明」, 并标注构造后是否可变。
5. 方法一览: 每行「方法名(参数) -> 返回类型: 一句话用途」, 仅作速查, 真正的参数说明仍写在方法自己的 docstring 里。
6. 构造参数: 完整的 `:param:` 与 `:type:` 成对列表。
7. `:return:` 与 `:rtype:`: 语义为该类构造完成后的实例。
8. `:raises:`: 语义为构造阶段可能抛出的异常。

栏目与编辑器可见性的对应关系:

| 栏目 | 悬停时提供的信息 |
| --- | --- |
| 一句话定位 | 这个类是什么、负责什么 |
| 工作流程 | 一次典型调用经历哪些步骤 |
| 实例属性 | 属性名、类型、含义、构造后是否可变 |
| 方法一览 | 每个公开方法及入参与返回, 仅作速查 |
| 构造参数 | 构造时必填与可选参数 |
| 构造返回值 | 构造完成的实例 |
| 构造异常 | 构造阶段可能抛出的异常 |

示例(以 `JsonExtractor` 为例):

```python
# =========** JsonExtractor =========**
class JsonExtractor:
    """按 json: 点路径从统一返回对象中取值, 一次运行内复用.

    工作流程:
        1. 构造时校验规则表非空, 并按名字浅拷贝到实例属性.
        2. pick 按名字取出规则, 得到 json:data.token 这类路径.
        3. 逐段下钻 dict / list; 任一段缺失即返回兜底值.
        4. 解析失败只记 WARNING, 不抛异常, 以免打断批量签到.

    实例属性:
        rules (dict[str, str]): 字段名到 json: 路径的映射, 构造后只读.
        default (Any): 调用方未传 default 时的兜底值, 构造后只读; 方法级 default 可覆盖, 不改实例字段.

    方法一览:
        pick(name, response, default) -> Optional[Any]: 按规则名取值, 失败收敛为兜底值.

    构造参数:
        :param rules: 字段名到 json: 路径的映射, 如 json:data.token
        :type rules: dict[str, str]
        :param default: 未命中时的兜底值, 默认 None
        :type default: Any

    构造返回值:
        :return: 构造完成的提取器实例
        :rtype: JsonExtractor

    构造异常:
        :raises ValueError: rules 为空时抛出, 视为配置错误
    """
```

说明: 上面的 `rules` 与 `default` 在 `__init__` 中必须有真实的属性类型标注, 编辑器据此提供补全与类型提示; docstring 里的 `:type:` 只作文档, 不参与类型推断。

## 附录：15.7 强制执行说明

> 本节不是原文正文的一部分, 仅为执行说明附录; 15.7 原文保持不动。

### 检查范围

- 覆盖: `.py` 源码、注释、docstring, 以及代码块内的 YAML/JSON 注释与文档中引用的代码片段。
- 不覆盖: 字符串字面量内部、二进制与生成文件、第三方源码。
- 检查项: 逗号、冒号、分号、括号、引号、句号、空格、连接号、感叹号与问号是否为半角。
- 书名号《》与直角引号「」: Markdown 文档正文允许, 代码注释与 docstring 禁用, 改用半角引号。

### 豁免标注方式

- 确需全角的字符串内容, 在命中行同一条注释内写 `豁免: <理由>`, 例如 `# 豁免: 站点文案原样保留`。
- 豁免只对标注所在行生效, 不覆盖整个文件。
- 无豁免标注的全角字符一律判违规, 不因口头说明文案需要而放行。

### 存量基线口径

- 首次执行按文件、行号、字符类别登记基线, 存档后作为门禁比较依据。
- 只许降不许升; 新增文件与新增改动行必须为 0 违规。
- 禁止为通过门禁批量替换既有注释标点; 存量按批次治理, 每批单独提交并复核语义。

## 附录：import 尾注对齐规则

> 本节不是规范正文的一部分, 仅为 15.2 与 15.8 的 import 尾注对齐补充执行细则; 正文判定不变。

- import 行尾注释符号 `#` 前保留 45 个空格, 以 15.2 与 15.8 的示例代码为准。
- 代码本身超过对齐位置时, 注释与代码之间至少保留一个空格。
- 使用 `ruff format` 或 `black` 之后必须人工核对对齐; 格式化不保证列位置。
- 提交前把对齐检查列入自检项。
