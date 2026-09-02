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
        for file in files:
            if path.exists(file):
                remove(file)
        remove(master_path)