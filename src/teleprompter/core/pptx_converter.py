"""PPTX → PDF 轉換：為右側投影片預覽提供統一的 PDF 路徑。

兩層策略（以可用性降級）：
1. Windows + 有 MS PowerPoint：用 comtypes 呼叫 PowerPoint COM，SaveAs 成 PDF。最快、最保真。
2. 有 LibreOffice（soffice 在 PATH）：`soffice --headless --convert-to pdf`。跨平台備援。
3. 都沒有：raise PptxConversionError，讓呼叫端顯示提示。

輸出 PDF 快取到 user data dir：
    %APPDATA%/SmartTeleprompter/cache/pptx/{hash}.pdf
快取命中條件：輸入檔 mtime ≤ 快取 mtime 即命中，避免重轉。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class PptxConversionError(RuntimeError):
    """PPTX 無法轉為 PDF。訊息給使用者看。"""


def _cache_dir() -> Path:
    """跨平台的 user cache dir。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / "SmartTeleprompter" / "cache" / "pptx"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(pptx_path: Path) -> str:
    """用絕對路徑 + mtime + size 做 hash，當作快取檔名。"""
    st = pptx_path.stat()
    raw = f"{pptx_path.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def convert_pptx_to_pdf(pptx_path: str | Path) -> Path:
    """把 PPTX 轉成 PDF，回傳 PDF 路徑（快取）。失敗 raise PptxConversionError。"""
    p = Path(pptx_path)
    if not p.exists():
        raise FileNotFoundError(f"PPTX 不存在: {p}")
    if p.suffix.lower() not in (".pptx", ".ppt"):
        raise ValueError(f"不是 PPTX/PPT 檔: {p}")

    cache_pdf = _cache_dir() / f"{_cache_key(p)}.pdf"
    if cache_pdf.exists() and cache_pdf.stat().st_mtime >= p.stat().st_mtime:
        logger.info("PPTX → PDF 命中快取: %s", cache_pdf)
        return cache_pdf

    # 1) PowerPoint COM（只在 Windows）
    if sys.platform == "win32":
        try:
            _convert_via_powerpoint(p, cache_pdf)
            return cache_pdf
        except Exception as e:
            logger.info("PowerPoint COM 轉檔失敗，嘗試 LibreOffice: %s", e)

    # 2) LibreOffice CLI
    soffice = _find_libreoffice()
    if soffice:
        try:
            _convert_via_libreoffice(soffice, p, cache_pdf)
            return cache_pdf
        except Exception as e:
            logger.warning("LibreOffice 轉檔失敗: %s", e)
            raise PptxConversionError(
                f"LibreOffice 轉檔失敗：{e}\n"
                "建議改成手動在 PowerPoint/Keynote 匯出 PDF 後再上傳。"
            ) from e

    raise PptxConversionError(
        "系統找不到 PowerPoint 或 LibreOffice 可用於 PPTX 轉檔。\n"
        "請擇一解決：\n"
        "  • 安裝 Microsoft PowerPoint（Windows）\n"
        "  • 安裝 LibreOffice（https://www.libreoffice.org/）\n"
        "  • 或自行把 PPTX 匯出成 PDF 後再上傳"
    )


def _convert_via_powerpoint(pptx: Path, out_pdf: Path) -> None:
    """用 COM 呼叫 PowerPoint → SaveAs(FileFormat=32 = ppSaveAsPDF)。"""
    import comtypes.client  # type: ignore

    # PowerPoint SaveAs 接受完整路徑
    abs_src = str(pptx.resolve())
    abs_dst = str(out_pdf.resolve())
    logger.info("PowerPoint COM 轉檔: %s → %s", abs_src, abs_dst)

    ppt = comtypes.client.CreateObject("PowerPoint.Application")
    try:
        # PowerPoint 2010+ 不允許完全不顯示；用 WithWindow=False
        pres = ppt.Presentations.Open(abs_src, WithWindow=False)
        try:
            # 32 = ppSaveAsPDF
            pres.SaveAs(abs_dst, 32)
        finally:
            pres.Close()
    finally:
        ppt.Quit()


def _find_libreoffice() -> str | None:
    """尋找 soffice 可執行檔。"""
    candidates: list[str] = []
    exe = "soffice.exe" if sys.platform == "win32" else "soffice"
    found = shutil.which(exe)
    if found:
        candidates.append(found)
    # Windows 常見安裝路徑
    if sys.platform == "win32":
        for base in (
            Path(os.environ.get("ProgramFiles", "C:/Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
        ):
            p = base / "LibreOffice" / "program" / "soffice.exe"
            if p.exists():
                candidates.append(str(p))
    # macOS
    elif sys.platform == "darwin":
        p = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if p.exists():
            candidates.append(str(p))
    return candidates[0] if candidates else None


def _convert_via_libreoffice(soffice: str, pptx: Path, out_pdf: Path) -> None:
    """soffice --headless --convert-to pdf --outdir <dir> <pptx>。
    soffice 會在 outdir 產生與原檔同名的 .pdf，我們再搬到快取名稱。
    """
    outdir = out_pdf.parent
    logger.info("LibreOffice 轉檔: %s (outdir=%s)", pptx, outdir)
    cmd = [
        soffice,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(outdir),
        str(pptx.resolve()),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"soffice exit {result.returncode}: {result.stderr or result.stdout}"
        )
    produced = outdir / f"{pptx.stem}.pdf"
    if not produced.exists():
        raise RuntimeError(f"LibreOffice 未產生 PDF: {produced}")
    if produced != out_pdf:
        # LibreOffice 用原檔名；rename 成我們的快取名
        if out_pdf.exists():
            out_pdf.unlink()
        produced.rename(out_pdf)
