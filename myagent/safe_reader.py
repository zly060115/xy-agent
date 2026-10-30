"""
安全的本地文件解析入库工具

安全规则（硬性约束，不可绕过）：
1. 只允许读取 config 中指定的白名单目录，其它路径一律拒绝
2. 只读：任何写、删、改文件的尝试都会被拒绝（本模块不提供写接口）
3. 必须经过人工确认：Agent 调用时不能直接执行，要先生成待确认请求
4. 禁止遍历系统目录（Windows / System32 / Program Files / 用户根目录等）
"""

import os
import re
from pathlib import Path

# 允许解析的扩展名
ALLOWED_EXT = {".txt", ".md", ".pdf", ".docx", ".doc"}

# 可以当图片读的扩展名
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

# 绝对禁止触碰的目录（即使被配置进白名单也强制排除）
FORBIDDEN_DIRS = {
    "windows",
    "system32",
    "syswow64",
    "program files",
    "program files (x86)",
    "programdata",
    "$recycle.bin",
    "appdata",
    "node_modules",
    ".git",
}

# 单文件大小上限：50MB
MAX_FILE_SIZE = 50 * 1024 * 1024


class FileAccessDenied(PermissionError):
    """文件访问被安全策略拒绝"""


def _norm(p: Path) -> str:
    return str(p.resolve()).lower().replace("\\", "/")


class SafeFileReader:
    """
    只读文件读取器。
    所有对外暴露的方法都不会修改文件，只做读取。
    """

    def __init__(
        self,
        allowed_dirs: list[str] | None = None,
        vision=None,
        ocr_enabled: bool = True,
        max_images: int = 12,
        vision_max_images: int = 6,
    ):
        raw = allowed_dirs or os.getenv("ALLOWED_FILE_DIRS", "").split(os.pathsep)
        self.allowed_dirs = []
        for d in raw:
            d = (d or "").strip()
            if not d:
                continue
            p = Path(d).expanduser()
            if p.exists() and p.is_dir():
                self.allowed_dirs.append(p.resolve())

        # 文档里图片的识别能力（vision 是能看图的模型，没有就只走 OCR）
        self.vision = vision
        self.ocr_enabled = ocr_enabled
        self.max_images = max(0, int(max_images))
        self.vision_max_images = max(0, int(vision_max_images))

    # ---------- 安全校验 ----------

    def _check_path(self, file_path: str) -> Path:
        """校验路径合法性，不合法直接抛异常"""
        p = Path(file_path).expanduser()

        if not p.is_absolute():
            raise FileAccessDenied(f"必须使用绝对路径: {file_path}")

        if not p.exists():
            raise FileAccessDenied(f"文件不存在: {file_path}")

        if p.is_dir():
            raise FileAccessDenied(f"这是目录不是文件: {file_path}")

        # 禁止符号链接（防止绕过白名单）
        if p.is_symlink():
            raise FileAccessDenied(f"拒绝访问符号链接: {file_path}")

        real = p.resolve()

        # 1. 必须落在白名单目录内
        if not self.allowed_dirs:
            raise FileAccessDenied(
                "未配置任何允许访问的目录。请在 .env 中设置 ALLOWED_FILE_DIRS"
            )

        inside = False
        for base in self.allowed_dirs:
            try:
                real.relative_to(base)
                inside = True
                break
            except ValueError:
                continue
        if not inside:
            raise FileAccessDenied(
                f"路径不在允许的目录内，已拒绝: {real}\n"
                f"允许的目录: {[str(d) for d in self.allowed_dirs]}"
            )

        # 2. 路径中任何一段命中禁止目录 → 拒绝
        for part in real.parts:
            if part.lower() in FORBIDDEN_DIRS:
                raise FileAccessDenied(f"路径包含受保护目录 {part}，已拒绝")

        # 3. 扩展名白名单（图片单独一类：只做识别，不入库）
        suffix = real.suffix.lower()
        if suffix not in ALLOWED_EXT and suffix not in IMAGE_EXT:
            raise FileAccessDenied(
                f"不支持的文件类型 {real.suffix}，"
                f"仅支持 {sorted(ALLOWED_EXT | IMAGE_EXT)}"
            )

        # 4. 文件大小
        size = real.stat().st_size
        if size > MAX_FILE_SIZE:
            raise FileAccessDenied(
                f"文件过大 ({size / 1024 / 1024:.1f}MB)，上限 {MAX_FILE_SIZE // 1024 // 1024}MB"
            )

        return real

    # ---------- 解析 ----------

    def read_text(self, file_path: str, progress=None) -> str:
        """读取文件纯文本内容（只读，不产生任何写入）

        progress: 可选进度回调 fn(text)，识别图片较慢时用来提示界面
        """
        real = self._check_path(file_path)
        suffix = real.suffix.lower()

        if suffix in (".txt", ".md"):
            return self._read_plain(real)
        if suffix == ".pdf":
            return self._read_pdf(real, progress)
        if suffix == ".docx":
            return self._read_docx(real, progress)
        if suffix == ".doc":
            return self._read_doc(real, progress)
        if suffix in IMAGE_EXT:
            return self._read_image_file(real, progress)
        raise FileAccessDenied(
            f"不支持的格式: {suffix}（支持 .txt .md .pdf .docx .doc 和常见图片）"
        )

    def _read_image_file(self, p: Path, progress=None) -> str:
        """单张图片：OCR 抽字，抽不出字再让视觉模型描述"""
        from .image_reader import read_image

        if progress:
            progress(f"正在识别图片 {p.name}…")

        try:
            data = p.read_bytes()
        except OSError as e:
            raise FileAccessDenied(f"图片读取失败: {p.name}") from e

        res = read_image(data, self.vision, allow_vision=self.vision_max_images > 0)
        if not res["text"]:
            raise FileAccessDenied(
                f"这张图片没有识别出文字或可描述的内容: {p.name}"
                "（可能是纯色图、尺寸过小的图标，或分辨率太低）"
            )
        return res["text"]

    @staticmethod
    def _read_plain(p: Path) -> str:
        for enc in ("utf-8", "gbk", "utf-16", "latin-1"):
            try:
                return p.read_text(encoding=enc)
            except (UnicodeDecodeError, LookupError):
                continue
        raise FileAccessDenied(f"无法识别文件编码: {p.name}")

    # ---------- 文档内嵌图片 ----------

    @staticmethod
    def _natural_key(name: str):
        """让 image2 排在 image10 前面"""
        return [int(s) if s.isdigit() else s.lower() for s in re.split(r"(\d+)", name)]

    def _docx_images(self, p: Path) -> list[tuple[str, bytes]]:
        """docx 本质是 zip，图片都在 word/media/ 下，直接读字节"""
        import zipfile

        out: list[tuple[str, bytes]] = []
        try:
            with zipfile.ZipFile(str(p)) as z:
                names = [n for n in z.namelist() if n.startswith("word/media/")]
                names.sort(key=self._natural_key)
                for n in names:
                    if Path(n).suffix.lower() not in IMAGE_EXT:
                        continue
                    try:
                        out.append((Path(n).name, z.read(n)))
                    except Exception:
                        continue
        except zipfile.BadZipFile:
            return []
        return out

    def _pdf_images(self, reader) -> list[tuple[str, bytes]]:
        """逐页抽内嵌图片（pypdf 的 page.images）"""
        out: list[tuple[str, bytes]] = []
        for i, page in enumerate(reader.pages):
            if len(out) >= self.max_images:
                break
            try:
                for img in page.images:
                    if len(out) >= self.max_images:
                        break
                    data = getattr(img, "data", None)
                    if data:
                        out.append((f"第{i + 1}页-{getattr(img, 'name', 'image')}", data))
            except Exception:
                continue
        return out

    def _images_to_section(self, images: list[tuple[str, bytes]], progress=None) -> str:
        """把图片逐张读成文字，拼成一个独立章节交给分析器"""
        if not images or self.max_images <= 0:
            return ""
        try:
            return self._images_to_section_inner(images, progress)
        finally:
            # 图片读完后把视觉模型从显存卸掉，把位置让给文本模型
            from .image_reader import release_vision

            release_vision(self.vision)

    def _images_to_section_inner(
        self, images: list[tuple[str, bytes]], progress=None
    ) -> str:
        from .image_reader import ocr_status, read_image

        if self.ocr_enabled and ocr_status().startswith("unavailable"):
            return (
                f"[提示：文档含 {len(images)} 张图片，但 OCR 引擎不可用，"
                f"这些图片的内容没有纳入分析]"
            )

        picked = images[: self.max_images]
        blocks: list[str] = []
        blank = 0
        vision_left = self.vision_max_images

        for i, (_name, data) in enumerate(picked, 1):
            if progress:
                progress(f"正在识别文档里的图片 {i}/{len(picked)}…")
            res = read_image(data, self.vision, allow_vision=vision_left > 0)
            if res["via"] in ("vision", "both"):
                vision_left -= 1
            if res["text"]:
                # 用粗体而不是 ### ：井号会被当成章节标题，把一章内容切碎
                blocks.append(f"**图 {i}**\n{res['text']}")
            else:
                blank += 1

        if not blocks:
            return (
                f"[提示：文档含 {len(images)} 张图片，但没有一张识别出可读内容，"
                f"这些图片没有纳入分析]"
            )

        head = (
            f"## 文档内嵌图片与图表内容\n\n"
            f"以下内容由文字识别与图像理解从文档的 {len(picked)} 张图中提取，"
            f"按文档里出现的顺序排列。"
        )
        out = head + "\n\n" + "\n\n".join(blocks)

        rest = len(images) - len(picked)
        if rest > 0:
            out += f"\n\n[提示：文档另有 {rest} 张图片超出单次处理上限，未纳入分析]"
        if blank:
            out += f"\n\n[提示：其中 {blank} 张图片没有识别出可读内容（可能是纯图形或装饰图）]"
        return out

    # ---------- 各格式解析 ----------

    def _read_pdf(self, p: Path, progress=None) -> str:
        from pypdf import PdfReader

        reader = PdfReader(str(p))
        total = len(reader.pages)
        pages = []
        for i, page in enumerate(reader.pages):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                pages.append(f"[第 {i + 1} 页]\n{t.strip()}")

        section = self._images_to_section(self._pdf_images(reader), progress)

        if not pages:
            # 整份都是扫描页：没有文字层，只能靠图片 OCR 兜底
            if section:
                return (
                    "（本 PDF 没有可提取的文字层，以下是图片识别结果）\n\n" + section
                )
            raise FileAccessDenied(f"PDF 中没有可提取的文字（可能是扫描件）: {p.name}")

        text = "\n\n".join(pages)
        blank = total - len(pages)
        if blank:
            text += (
                f"\n\n[提示：PDF 共 {total} 页，其中 {blank} 页没有可提取的文字"
                f"（可能是扫描件或纯图片页），这些页的内容由图片识别补充。]"
            )
        if section:
            text += "\n\n" + section
        return text

    def _read_docx(self, p: Path, progress=None) -> str:
        import docx

        doc = docx.Document(str(p))
        parts = [para.text for para in doc.paragraphs if para.text.strip()]

        # 表格内容也提取
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))

        # 图片：OCR 抽字，抽不出的交给视觉模型看
        section = self._images_to_section(self._docx_images(p), progress)

        if not parts and not section:
            raise FileAccessDenied(f"docx 中没有文字内容: {p.name}")

        text = "\n\n".join(parts)
        if section:
            text = (text + "\n\n" + section) if text else section
        elif not self.ocr_enabled:
            imgs = sum(1 for rel in doc.part.rels.values() if "image" in rel.reltype)
            if imgs:
                text += f"\n\n[提示：本文档含 {imgs} 张图片，图片识别已关闭，图片内容未纳入分析]"
        return text

    def _read_doc(self, p: Path, progress=None) -> str:
        """.doc 是 97-2003 老二进制格式，解析库读不了。

        做法：调用系统里安装的 Word 或 WPS 的 COM 接口，
        在系统临时目录里转出一份 .docx 副本再解析。
        原文件全程只读，不修改、不移动。
        """
        import base64
        import subprocess
        import tempfile

        if not p.exists():
            raise FileAccessDenied(f"文件不存在: {p.name}")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / (p.stem + ".docx")
            src = str(p).replace("'", "''")
            dst = str(out).replace("'", "''")

            ps = f'''
$ErrorActionPreference = "Stop"
$app = $null
foreach ($progid in @("Word.Application", "KWPS.Application", "WPS.Application")) {{
    try {{ $app = New-Object -ComObject $progid; break }} catch {{ }}
}}
if ($null -eq $app) {{ throw "NO_OFFICE" }}
try {{
    $app.Visible = $false
    $app.DisplayAlerts = 0
    $doc = $app.Documents.Open('{src}', $false, $true)
    if ($null -eq $doc) {{ throw "OPEN_FAILED" }}
    try {{
        $doc.SaveAs2('{dst}', 16)
    }} finally {{
        $doc.Close($false)
    }}
}} finally {{
    $app.Quit()
}}
'''
            b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
            try:
                # 不用 text=True：PowerShell 输出是 GBK，按 UTF-8 解码会崩
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-EncodedCommand", b64],
                    capture_output=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired as e:
                raise FileAccessDenied(
                    f"doc 转换超时（Office/WPS 可能弹了对话框）: {p.name}"
                ) from e

            stderr = (r.stderr or b"").decode("gbk", errors="replace")
            if r.returncode != 0 or not out.exists():
                if "NO_OFFICE" in stderr:
                    raise FileAccessDenied(
                        f"电脑上没找到 Word 或 WPS，无法解析 .doc: {p.name}"
                        "（另存为 .docx 后可直接上传）"
                    )
                raise FileAccessDenied(
                    f"doc 转换失败: {p.name}（建议另存为 .docx）"
                )
            return self._read_docx(out, progress)

    # ---------- 目录浏览（只读） ----------

    def list_allowed_files(self) -> list[dict]:
        """列出白名单目录下的可解析文件（不递归进禁止目录）"""
        out = []
        for base in self.allowed_dirs:
            for p in base.rglob("*"):
                if not p.is_file() or p.is_symlink():
                    continue
                if p.suffix.lower() not in ALLOWED_EXT:
                    continue
                if any(part.lower() in FORBIDDEN_DIRS for part in p.parts):
                    continue
                try:
                    out.append(
                        {
                            "path": str(p),
                            "name": p.name,
                            "size": p.stat().st_size,
                        }
                    )
                except OSError:
                    continue
        return out

    def info(self) -> dict:
        return {
            "allowed_dirs": [str(d) for d in self.allowed_dirs],
            "allowed_ext": sorted(ALLOWED_EXT),
            "max_size_mb": MAX_FILE_SIZE // 1024 // 1024,
        }
