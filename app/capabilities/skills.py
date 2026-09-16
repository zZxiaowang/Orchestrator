"""Skills：``SKILL.md`` 的解析、资格校验、安装与"按需注入"。

形态对齐事实标准（Codex 与 Claude 的 skill 都是这个结构），所以市面上大多数 skill 可以直接装::

    <skill>/
      SKILL.md    必需：frontmatter(name / description / 可选 version / triggers / allowed-tools) + 正文指令
      scripts/    可选：会被登记但默认不执行（要跑走命令白名单 + 每次确认）
      references/ assets/  可选：按需读取

安装 = 复制进 ``data/capabilities/skills/<id>/``：源目录被移动或删除也不影响已装能力，
执行段注入时只读本地这一份。

安全边界：

* 只复制文本类文件，单文件 / 总大小 / 文件数都有上限；
* 不跟随符号链接，不接受越界路径；
* 安装本身不执行任何东西；``scripts/`` 只登记进 permissions 与 meta。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from app.core.errors import AppError
from app.schemas.capability import Capability, CapabilityKind, CapabilityScope, CapabilitySource

SKILL_FILE = "SKILL.md"
MAX_SKILL_MD_BYTES = 64 * 1024
MAX_SKILL_FILES = 200
MAX_SKILL_BYTES = 5 * 1024 * 1024
MAX_BODY_CHARS = 20000

# 允许随 skill 一起装进来的子目录（其余文件只在根目录）
KNOWN_SUBDIRS = ("scripts", "references", "assets")

# 注入执行段时：最多几条 skill、总共多少字符
INJECT_MAX_SKILLS = 2
INJECT_MAX_CHARS = 2400
INJECT_PER_SKILL_CHARS = 1600

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


@dataclass
class ParsedSkill:
    """一份解析好的 SKILL.md。"""

    id: str
    name: str
    description: str
    version: str = ""
    body: str = ""
    triggers: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    scripts: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def slugify_skill_id(value: str) -> str:
    """把名字压成合法 ID；压不出来（纯中文等）就退回 ``skill-<hash>``。"""

    slug = re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
    slug = slug[:48].strip("-._")
    if slug and _ID_RE.match(slug):
        return slug
    digest = hashlib.sha1((value or "skill").encode("utf-8")).hexdigest()[:8]
    return f"skill-{digest}"


def parse_skill_md(text: str, *, fallback_id: str = "") -> ParsedSkill:
    """解析 frontmatter 与正文；缺 name / description 直接报错（资格校验）。"""

    if not text.strip():
        raise AppError(f"{SKILL_FILE} 是空的。", code="invalid_skill")
    if len(text.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise AppError(
            f"{SKILL_FILE} 过大（上限 {MAX_SKILL_MD_BYTES // 1024} KB）。", code="invalid_skill"
        )

    front: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise AppError(
                f"{SKILL_FILE} 的 frontmatter 不是合法 YAML：{str(exc).splitlines()[0]}",
                code="invalid_skill",
            ) from exc
        if not isinstance(loaded, dict):
            raise AppError(f"{SKILL_FILE} 的 frontmatter 必须是键值对。", code="invalid_skill")
        front = {str(key).strip().lower(): value for key, value in loaded.items()}
        body = text[match.end() :]

    name = str(front.get("name") or "").strip()
    description = str(front.get("description") or "").strip()
    if not name:
        raise AppError(f"{SKILL_FILE} 的 frontmatter 缺少 name（技能名）。", code="invalid_skill")
    if not description:
        raise AppError(
            f"{SKILL_FILE} 的 frontmatter 缺少 description（一句话说明这个技能做什么）。",
            code="invalid_skill",
        )

    raw_triggers = front.get("triggers") or front.get("keywords") or []
    if isinstance(raw_triggers, str):
        raw_triggers = re.split(r"[,\s]+", raw_triggers)
    triggers = [str(item).strip().lower() for item in raw_triggers if str(item).strip()]
    if not triggers:
        # 没写 triggers 就用名字兜底：名字本身通常就是最好的触发词
        triggers = [name.lower()]

    return ParsedSkill(
        id=slugify_skill_id(str(front.get("id") or fallback_id or name)),
        name=name[:80],
        description=description[:400],
        version=str(front.get("version") or "").strip()[:40],
        body=body.strip(),
        triggers=triggers,
        frontmatter=front,
    )


def load_skill_dir(source: Path) -> ParsedSkill:
    """校验一个目录是不是合格的 skill，返回解析结果（含文件清单）。"""

    root = Path(source)
    if not root.is_dir():
        raise AppError(f"技能目录不存在：{root}", code="invalid_skill")
    skill_md = root / SKILL_FILE
    if not skill_md.is_file():
        raise AppError(f"目录里没有 {SKILL_FILE}（它必须是技能的入口）。", code="invalid_skill")

    parsed = parse_skill_md(
        skill_md.read_text(encoding="utf-8", errors="replace"), fallback_id=root.name
    )

    files: list[str] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            raise AppError(f"技能里不允许符号链接：{path.name}", code="invalid_skill")
        relative = path.relative_to(root).as_posix()
        total += path.stat().st_size
        if total > MAX_SKILL_BYTES:
            raise AppError(
                f"技能体积过大（上限 {MAX_SKILL_BYTES // (1024 * 1024)} MB）。",
                code="invalid_skill",
            )
        if len(files) >= MAX_SKILL_FILES:
            raise AppError(f"技能文件过多（上限 {MAX_SKILL_FILES} 个）。", code="invalid_skill")
        files.append(relative)

    parsed.files = files
    parsed.scripts = [item for item in files if item.startswith("scripts/")]
    return parsed


def skill_permissions(parsed: ParsedSkill) -> list[str]:
    permissions = ["instructions"]
    if parsed.scripts:
        permissions.append("scripts")
    if any(item.startswith("references/") for item in parsed.files):
        permissions.append("references")
    return permissions


def read_skill_body(skill_dir: Path, *, limit: int = MAX_BODY_CHARS) -> str:
    """读回已装技能的正文（注入上下文与界面预览都用它）。"""

    path = Path(skill_dir) / SKILL_FILE
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        body = parse_skill_md(text, fallback_id=Path(skill_dir).name).body
    except AppError:
        body = text
    if len(body) <= limit:
        return body
    return body[:limit] + f"\n…（已截断，共 {len(body)} 字符）"


# ── 安装 ──


def install_dir(root: Path, skill_id: str) -> Path:
    return Path(root) / "skills" / skill_id


def _capability_of(
    parsed: ParsedSkill,
    target: Path,
    *,
    origin: str,
    scope: CapabilityScope,
    project_id: str,
    enabled: bool,
) -> Capability:
    kind, _, location = origin.partition(":")
    return Capability(
        id=parsed.id,
        kind=CapabilityKind.SKILL,
        name=parsed.name,
        description=parsed.description,
        version=parsed.version,
        enabled=enabled,
        scope=scope,
        project_id=project_id if scope is CapabilityScope.PROJECT else "",
        permissions=skill_permissions(parsed),
        source=CapabilitySource(kind=kind, location=location or origin),  # type: ignore[arg-type]
        meta={
            "entry": SKILL_FILE,
            "path": str(target),
            "body_chars": len(parsed.body),
            "scripts": parsed.scripts,
            "files": parsed.files,
            "triggers": parsed.triggers,
        },
    )


def _copy_into(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=False)
    shutil.copytree(Path(source), target)
    return target


def install_local(
    source: Path,
    *,
    root: Path,
    scope: CapabilityScope = CapabilityScope.GLOBAL,
    project_id: str = "",
    enabled: bool = True,
) -> Capability:
    """从本地目录安装（复制进数据目录，源目录之后可以随便动）。"""

    parsed = load_skill_dir(source)
    target = _copy_into(source, install_dir(root, parsed.id))
    return _capability_of(
        parsed,
        target,
        origin=f"local:{Path(source)}",
        scope=scope,
        project_id=project_id,
        enabled=enabled,
    )


def install_extracted(
    extracted: Path,
    *,
    root: Path,
    origin: str,
    scope: CapabilityScope = CapabilityScope.GLOBAL,
    project_id: str = "",
    enabled: bool = True,
) -> Capability:
    """从解压/下载得到的目录安装（GitHub 与 zip 走这里）。"""

    parsed = load_skill_dir(extracted)
    target = _copy_into(extracted, install_dir(root, parsed.id))
    return _capability_of(
        parsed, target, origin=origin, scope=scope, project_id=project_id, enabled=enabled
    )


async def fetch_zip(url: str, *, timeout: float = 60.0, transport: Any = None) -> Path:
    """下载一个 zip 并解压到临时目录，返回解压根（调用方负责清理父目录）。"""

    if not url.lower().startswith(("http://", "https://")):
        raise AppError(f"只接受 http(s) 地址：{url}", code="invalid_skill_source")
    workdir = Path(tempfile.mkdtemp(prefix="skill-dl-"))
    archive = workdir / "skill.zip"
    async with httpx.AsyncClient(
        timeout=timeout, transport=transport, follow_redirects=True
    ) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(
                f"下载技能包失败：{url}（{exc.__class__.__name__}: {exc}）",
                code="skill_download_failed",
            ) from exc
    archive.write_bytes(response.content)
    if archive.stat().st_size > MAX_SKILL_BYTES * 4:
        raise AppError("技能包过大（压缩包上限 20 MB）。", code="invalid_skill")
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(workdir / "unpacked")
    except zipfile.BadZipFile as exc:
        raise AppError("下载到的不是合法 zip 包。", code="invalid_skill") from exc
    return workdir / "unpacked"


def find_skill_root(extracted: Path) -> Path:
    """在解压结果里找 SKILL.md 所在目录：根目录没有就往下找一个唯一子目录。"""

    if (extracted / SKILL_FILE).is_file():
        return extracted
    children = [item for item in extracted.iterdir() if item.is_dir()]
    for child in children:
        if (child / SKILL_FILE).is_file():
            return child
    if len(children) == 1:
        return find_skill_root(children[0])
    raise AppError(f"解压结果里没有找到 {SKILL_FILE}。", code="invalid_skill")


def parse_github_location(location: str) -> tuple[str, str, str, str]:
    """把 ``owner/repo[#ref][/sub/dir]``（或 GitHub 网址）拆成四部分。"""

    text = (location or "").strip()
    if not text:
        raise AppError(
            "GitHub 来源不能为空，形如 owner/repo 或 owner/repo#main/sub/dir。",
            code="invalid_skill_source",
        )
    if text.startswith(("http://", "https://")):
        text = urlparse(text).path.strip("/")
        if text.endswith(".git"):
            text = text[:-4]
    if text.startswith("github.com/"):
        text = text[len("github.com/") :]
    ref = ""
    subpath = ""
    if "#" in text:
        text, _, tail = text.partition("#")
        ref, _, subpath = tail.partition("/")
    parts = [item for item in text.split("/") if item]
    if len(parts) < 2:
        raise AppError("GitHub 来源要写成 owner/repo[#ref][/子目录]。", code="invalid_skill_source")
    owner, repo = parts[0], parts[1]
    if len(parts) > 2 and not subpath:
        subpath = "/".join(parts[2:])
    return owner, repo, ref or "HEAD", subpath


async def install_from_github(
    location: str,
    *,
    root: Path,
    transport: Any = None,
    scope: CapabilityScope = CapabilityScope.GLOBAL,
    project_id: str = "",
    enabled: bool = True,
) -> Capability:
    """按 ``owner/repo[#ref][/子目录]`` 装：下 zip、找 SKILL.md、复制进数据目录。"""

    owner, repo, ref, subpath = parse_github_location(location)
    url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    extracted = await fetch_zip(url, transport=transport)
    try:
        found = find_skill_root(extracted)
        if subpath:
            candidate = found / subpath
            if not candidate.is_dir():
                raise AppError(f"仓库里没有这个子目录：{subpath}", code="invalid_skill_source")
            found = candidate
        return install_extracted(
            found,
            root=root,
            origin=f"github:{owner}/{repo}#{ref}" + (f"/{subpath}" if subpath else ""),
            scope=scope,
            project_id=project_id,
            enabled=enabled,
        )
    finally:
        shutil.rmtree(extracted.parent, ignore_errors=True)


async def install_from_zip_url(
    url: str,
    *,
    root: Path,
    transport: Any = None,
    scope: CapabilityScope = CapabilityScope.GLOBAL,
    project_id: str = "",
    enabled: bool = True,
) -> Capability:
    extracted = await fetch_zip(url, transport=transport)
    try:
        return install_extracted(
            find_skill_root(extracted),
            root=root,
            origin=f"https:{url}",
            scope=scope,
            project_id=project_id,
            enabled=enabled,
        )
    finally:
        shutil.rmtree(extracted.parent, ignore_errors=True)


# ── 按需注入 ──


def select_skills(
    skills: list[Capability], *, text: str, limit: int = INJECT_MAX_SKILLS
) -> list[Capability]:
    """按触发词挑这一步要用哪些 skill（匹配不到就不注入，别白烧上下文）。"""

    haystack = (text or "").lower()
    if not haystack.strip():
        return []
    scored: list[tuple[int, str, Capability]] = []
    for capability in skills:
        if capability.kind is not CapabilityKind.SKILL or not capability.enabled:
            continue
        triggers = [str(item).lower() for item in (capability.meta.get("triggers") or [])]
        if not triggers:
            triggers = [capability.name.lower(), capability.id.lower()]
        hits = sum(1 for trigger in triggers if trigger and trigger in haystack)
        if capability.id.lower() in haystack:
            hits += 1
        if hits:
            scored.append((hits, capability.id, capability))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[: max(0, limit)]]


def build_skill_section(skills: list[Capability]) -> str:
    """把选中的 skill 拼成一段可注入的上下文（总量与单条都有上限）。"""

    if not skills:
        return ""
    chunks: list[str] = []
    used = 0
    for capability in skills:
        body = read_skill_body(
            Path(str(capability.meta.get("path") or "")), limit=INJECT_PER_SKILL_CHARS
        )
        if not body:
            continue
        piece = f"### 技能：{capability.name}\n{body}"
        if used + len(piece) > INJECT_MAX_CHARS:
            break
        chunks.append(piece)
        used += len(piece)
    if not chunks:
        return ""
    return "## 可用技能（系统按触发词挑出来的，仅供参考，不必复述）\n\n" + "\n\n".join(chunks)
