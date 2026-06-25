"""评测套件

``gsuid_core`` 框架的离线评测脚本集合，按数据集分目录：

- :mod:`eval.common`          : 评测公共模块（HTTP 客户端、IO、LLM 评判）
- :mod:`eval.longmemeval`     : LongMemEval-S 评测入口
- :mod:`eval.BEAM_10M`        : BEAM-10M 评测入口

运行方式：

.. code-block:: bash

    # 方式一：从项目根以模块方式运行（推荐，依赖 PYTHONPATH 含项目根）
    python -m eval.longmemeval.run_longmem_eval run --base-url http://127.0.0.1:8765

    # 方式二：直接执行脚本（脚本内部把项目根加入 sys.path）
    python eval/longmemeval/run_longmem_eval.py run --base-url http://127.0.0.1:8765
"""
