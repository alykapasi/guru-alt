"""XLSX adapter — one located unit per worksheet (openpyxl)."""

from pathlib import Path

from openpyxl import load_workbook

from app.rag.adapters.base import ExtractContext, ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class XlsxAdapter:
    name = "xlsx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    async def extract(self, path: Path, *, meta: dict, ctx: ExtractContext) -> list[ExtractedUnit]:
        # Pass a file handle (not the path) so openpyxl skips its extension check — our temp
        # file has no .xlsx suffix. read_only is lazy, so keep the handle open while iterating.
        with open(path, "rb") as handle:
            workbook = load_workbook(handle, read_only=True, data_only=True)
            try:
                units: list[ExtractedUnit] = []
                for worksheet in workbook.worksheets:
                    rows = [
                        " ".join(str(cell) for cell in row if cell is not None)
                        for row in worksheet.iter_rows(values_only=True)
                    ]
                    text = "\n".join(row for row in rows if row.strip())
                    if text.strip():
                        units.append(ExtractedUnit(text=text, locator={"sheet": worksheet.title}))
                return units
            finally:
                workbook.close()
