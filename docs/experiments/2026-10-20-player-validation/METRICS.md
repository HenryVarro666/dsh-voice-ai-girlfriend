# 浏览器原始事件可重算指标

起点为最后一个有声输入采样，终点为第一个有声输出采样的AudioContext排定时刻；不含硬件。

| 轮次 | 候选 | 路径 | 断轮/STT ms | DSH/LLM ms | TTS/播放 ms | 总计 ms |
|---|---|---|---:|---:|---:|---:|
| first-new-session | baseline | fallback | 1306.5 | 2081.5 | 365.5 | 3753.5 |
| case-0-repeat-0 | baseline | fallback | 1281.9 | 115.8 | 406.3 | 1804.0 |
| case-0-repeat-0 | guard3 | fallback | 1282.7 | 127.5 | 411.3 | 1821.5 |
| case-1-repeat-0 | guard3 | fast | 649.2 | 117.2 | 274.2 | 1040.6 |
| case-1-repeat-0 | baseline | fast | 632.1 | 172.0 | 258.9 | 1063.0 |
| case-2-repeat-0 | baseline | fallback | 1244.9 | 154.3 | 469.3 | 1868.5 |
| case-2-repeat-0 | guard3 | fallback | 1265.0 | 137.3 | 608.0 | 2010.3 |
| case-3-repeat-0 | guard3 | fast | 636.8 | 143.9 | 401.8 | 1182.5 |
| case-3-repeat-0 | baseline | fast | 620.3 | 136.0 | 545.2 | 1301.5 |
| case-0-repeat-1 | guard3 | fallback | 1296.4 | 121.4 | 332.2 | 1750.1 |
| case-0-repeat-1 | baseline | fallback | 1258.0 | 117.2 | 436.8 | 1812.0 |
| case-1-repeat-1 | baseline | fast | 626.6 | 125.1 | 328.1 | 1079.8 |
| case-1-repeat-1 | guard3 | fast | 609.9 | 178.0 | 354.4 | 1142.3 |
| case-2-repeat-1 | guard3 | fallback | 1317.7 | 121.2 | 516.7 | 1955.6 |
| case-2-repeat-1 | baseline | fallback | 1302.9 | 151.3 | 456.7 | 1910.9 |
| case-3-repeat-1 | baseline | fast | 666.4 | 105.0 | 356.2 | 1127.7 |
| case-3-repeat-1 | guard3 | fast | 602.5 | 152.5 | 340.0 | 1095.0 |

## 分路径汇总

| 组别 | n | 中位数 ms | 最小 ms | 最大 ms |
|---|---:|---:|---:|---:|
| baseline-fast | 4 | 1103.7 | 1063.0 | 1301.5 |
| baseline-fallback | 4 | 1840.3 | 1804.0 | 1910.9 |
| baseline-all | 8 | 1552.8 | 1063.0 | 1910.9 |
| guard3-fast | 4 | 1118.6 | 1040.6 | 1182.5 |
| guard3-fallback | 4 | 1888.5 | 1750.1 | 2010.3 |
| guard3-all | 8 | 1466.3 | 1040.6 | 2010.3 |

all混合两类等待路径，其联合中位数可能与各路径的比较方向不同；不能据此声称保护器使首声加速。样本少，不提供稳定p95。
