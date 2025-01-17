from io import BytesIO
from typing import Any, BinaryIO, Dict, List, Optional

from services.path_fixer.fixpaths import clean_toc
from services.report.fixes import get_fixes_from_raw


class ParsedUploadedReportFile(object):
    def __init__(
        self,
        filename: Optional[str],
        file_contents: BinaryIO,
        labels: Optional[List[str]] = None,
    ):
        self.filename = filename
        self.contents = file_contents.getvalue()
        self.size = len(self.contents)
        self.labels = labels

    @property
    def file_contents(self):
        return BytesIO(self.contents)

    def get_first_line(self):
        return self.file_contents.readline()


class ParsedRawReport(object):
    def __init__(
        self,
        toc: Optional[BinaryIO],
        env: Optional[BinaryIO],
        uploaded_files: List[ParsedUploadedReportFile],
        path_fixes: Optional[BinaryIO],
    ):
        self.toc = toc
        self.env = env
        self.uploaded_files = uploaded_files
        self.path_fixes = path_fixes

    def has_toc(self) -> bool:
        return self.toc is not None

    def has_env(self) -> bool:
        return self.env is not None

    def has_path_fixes(self) -> bool:
        return self.path_fixes is not None

    @property
    def size(self):
        return sum(f.size for f in self.uploaded_files)

    def content(self) -> BytesIO:
        buffer = BytesIO()
        if self.has_toc():
            for file in self.get_toc():
                buffer.write(f"{file}\n".encode("utf-8"))
            buffer.write("<<<<<< network\n\n".encode("utf-8"))
        for file in self.uploaded_files:
            buffer.write(f"# path={file.filename}\n".encode("utf-8"))
            buffer.write(file.contents)
            buffer.write("\n<<<<<< EOF\n\n".encode("utf-8"))
        buffer.seek(0)
        return buffer


class VersionOneParsedRawReport(ParsedRawReport):
    def get_toc(self) -> List[str]:
        return self.toc

    def get_env(self):
        return self.env

    def get_uploaded_files(self):
        return self.uploaded_files

    def get_path_fixes(self, path_fixer) -> Dict[str, Dict[str, Any]]:
        return self.path_fixes


class LegacyParsedRawReport(ParsedRawReport):
    def get_toc(self) -> List[str]:
        toc = self.toc.read().decode(errors="replace").strip()
        toc = clean_toc(toc)
        self.toc.seek(0, 0)
        return toc

    def get_env(self):
        return self.env.read().decode(errors="replace")

    def get_uploaded_files(self):
        return self.uploaded_files

    def get_path_fixes(self, path_fixer) -> Dict[str, Dict[str, Any]]:
        path_fixes = self.path_fixes.read().decode(errors="replace")
        return get_fixes_from_raw(path_fixes, path_fixer)
