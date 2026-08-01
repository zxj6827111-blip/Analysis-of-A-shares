# 复权语义说明

## 三种复权方式

### 1. 未复权 (none)

原始交易价格，不做任何调整。用于 L2 执行和账户估值。

- volume 单位：股（local_vendor 已乘 100，tushare 已乘 100）
- amount 单位：元（local_vendor 已乘 1000，tushare 已乘 1000）

### 2. Tushare 因子前复权 (tushare_factor_qfq)

使用 Tushare `adj_factor` 派生的前复权价格：

```
qfq_price = raw_price × (factor / anchor_factor)
```

- `anchor_factor`：截止日期（cutoff）当日或之前最后一个有效因子
- `formula_version`: `ctsfqfq_v1`
- `anchor_policy`: `last_factor_on_or_before_cutoff`
- volume / amount **不复权**，直接复制原始值
- 精度：round_half_even 4 位小数存储，2 位小数比较

### 3. 通达信仿射前复权 (tdxquant/front)

通达信客户端原生前复权，使用仿射变换（含平移项）。

**已知问题**：长历史股票可能出现负价格。

- 负价格 **只能** 用于 L1 信号计算
- 负价格 **绝对不能** 进入 L2 成交和账户估值
- 因此 tdxquant/front 不能作为 L2 执行数据源

## 因子解析规则 (factor_resolution_v1)

composite QFQ 派生和回测 L3 复权因子门使用统一的四级解析：

```
exact_main > exact_supplement > alias_main > alias_supplement
```

| 层级 | 说明 |
|------|------|
| exact_main | 在主因子数据集中按原始代码精确匹配 |
| exact_supplement | 在补充因子数据集中按原始代码精确匹配（退市股） |
| alias_main | 通过 PIT 宇宙别名映射到 920 规范代码后在主数据集匹配（北交所迁移） |
| alias_supplement | 别名映射后在补充数据集匹配 |

所有层级均失败时返回 `quality="incomplete"`，下游 fail-closed。

## 数据集 lineage

每个 composite QFQ 数据集的 manifest 记录：

- `raw_dataset_id`：父 raw 数据集
- `factor_dataset_id`：父因子数据集
- `formula_version`：派生公式版本
- `anchor_policy`：锚点规则
- `supplement_factor_dataset_id`：补充因子数据集（如有）
- 父数据集 manifest SHA256
