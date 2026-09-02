# NSYSU MATH208: 延後匯入 CPPKernel。
# 直接 `from .__main__ import CPPKernel` 會讓 jcppkernel.toolchain 在 package
# 匯入時就進 sys.modules，之後 `python -m jcppkernel.toolchain` 會被 runpy 抱怨
# 「found in sys.modules ... prior to execution」。用 PEP 562 的 module __getattr__
# 保持 `from jcppkernel import CPPKernel` 可用，同時讓 -m 乾淨執行。

__all__ = ["CPPKernel"]


def __getattr__(name):
    if name == "CPPKernel":
        from .__main__ import CPPKernel
        return CPPKernel
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
