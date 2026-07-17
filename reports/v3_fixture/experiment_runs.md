# Experiment Runs

五个运行都记录 config path/hash、environment、git commit/dirty、seed、命令、起止时间、artifact hash 和状态 transition event。

| Experiment | Status | Validation accuracy | Parameters | Runtime (s) | Relation |
|---|---|---:|---:|---:|---|
| fixture-vision-exp-001 | completed | 0.916667 | 67 | 0.014336 | baseline |
| fixture-vision-exp-002 | completed | 0.979167 | 67 | 0.018429 | addition |
| fixture-vision-exp-003 | completed | 0.958333 | 145 | 0.015299 | concat projection |
| fixture-vision-exp-004 | failed | — | — | — | fixture-failure-001 |
| fixture-vision-exp-005 | completed | 0.937500 | 67 | 0.014963 | retry_of exp-004 |

重复执行没有新增记录；五个 Experiment 均为 unchanged。运行时间只描述本次 CPU Fixture，不用于科研结论。
