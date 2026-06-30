"""XLSX adapter — one located unit per worksheet (openpyxl)."""

import io

from openpyxl import load_workbook

from app.rag.adapters.base import ExtractedUnit

_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class XlsxAdapter:
    name = "xlsx"

    def handles(self, content_type: str) -> bool:
        return content_type == _CONTENT_TYPE

    def extract(self, data: bytes, *, meta: dict) -> list[ExtractedUnit]:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
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
