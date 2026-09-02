from os import remove, path
from tempfile import NamedTemporaryFile

class CPPTempFileProcessing:
    def _new_temp_file(self, files, **kwargs):
        # NSYSU MATH208: 明確指定 UTF-8。Windows 上 open() 預設走 locale encoding
        # （繁中是 cp950），cell 裡的中文字串會寫不進暫存 .cpp 或寫成 cp950，
        # 導致 g++ 讀到亂碼、或後續 decode 失敗。
        file = NamedTemporaryFile(delete=False, mode="w", encoding="utf-8", **kwargs)
        files.append(file.name)
        return file
    
    def _cleanup_files(self, master_path, files):
        # NSYSU MATH208: master_path 可能是 None（工具鏈還沒就緒），
        # 也可能已經在 files 裡（_build_master 會登記），所以兩邊都要防重複／防 None。
        for file in list(files) + [master_path]:
            if file and path.exists(file):
                try:
                    remove(file)
                except OSError:
                    pass