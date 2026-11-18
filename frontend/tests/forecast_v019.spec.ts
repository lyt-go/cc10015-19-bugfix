import { expect, test } from '@playwright/test'

test('V019 预测结果表：刷新稳定、批次可见、详情同源、失败保留完整结果', async ({ page }) => {
  test.setTimeout(120_000)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`)
  })

  await page.goto('/forecast_model')
  const rows = page.locator('tbody tr[data-testid^="prediction-row-"]')
  await expect(rows.first()).toBeVisible({ timeout: 10_000 })
  const firstSnapshot = await rows.allInnerTexts()
  const batch = await page.getByTestId('active-batch').innerText()
  const calibre = await page.getByTestId('active-calibre').innerText()
  expect(batch).not.toBe('—')
  expect(calibre).not.toBe('—')

  // 连续刷新同一筛选条件：列表内容不变，训练批次与口径仍然可见。
  for (let i = 0; i < 3; i++) {
    await page.getByTestId('refresh').click()
    await expect(rows.first()).toBeVisible()
    expect(await rows.allInnerTexts()).toEqual(firstSnapshot)
  }
  await expect(page.getByTestId('active-batch')).toHaveText(batch)
  await expect(page.getByTestId('active-calibre')).toHaveText(calibre)

  // 按训练批次筛选：只返回该批次，且批次号出现在表内。
  await page.getByTestId('filter-训练批次').fill('FB-202609-01')
  await page.getByTestId('query').click()
  await expect(rows).toHaveCount(6)
  for (const text of await rows.allInnerTexts()) expect(text).toContain('FB-202609-01')
  await expect(page.getByTestId('active-batch')).toHaveText('FB-202609-01')

  // 半成品 / 失败批次不返回预测点。
  await page.getByTestId('filter-训练批次').fill('FB-202609-03F')
  await page.getByTestId('query').click()
  await expect(rows).toHaveCount(0)
  await expect(page.getByTestId('prediction-empty')).toContainText('尚未完成')

  // 详情页读取同一条留档结果。
  await page.getByTestId('filter-训练批次').fill('FB-202609-01')
  await page.getByTestId('query').click()
  await expect(rows).toHaveCount(6)
  const firstRow = rows.first()
  const firstValue = await firstRow.locator('td').nth(5).innerText()
  await firstRow.getByRole('button', { name: '查看' }).click()
  const detail = page.getByTestId('prediction-detail')
  await expect(detail).toBeVisible()
  await expect(detail).toContainText('FB-202609-01')
  await expect(detail).toContainText(firstValue)
  await expect(detail).toContainText('展示值一致')

  // 最近完成批次遇到同周期双批次时给出对照；最早完整批次稳定直返。
  await page.getByTestId('reset').click()
  await page.getByTestId('filter-模型编号').fill('FORE-0001')
  await page.getByTestId('query').click()
  await expect(page.getByTestId('active-batch')).toHaveText('FB-202609-02')
  await expect(page.getByTestId('conflict-notice')).toContainText('FB-202609-01')
  await page.getByTestId('toggle-comparison').click()
  await expect(page.getByTestId('comparison-list')).toContainText('FB-202609-01')
  await page.getByTestId('filter-policy').selectOption('earliest_complete')
  await page.getByTestId('query').click()
  await expect(page.getByTestId('active-batch')).toHaveText('FB-202609-01')

  // 重新训练失败：列表维持最近一次完整预测结果，不出现半成品批次。
  await page.getByTestId('reset').click()
  const before = await rows.allInnerTexts()
  const beforeBatch = await page.getByTestId('active-batch').innerText()
  await page.getByTestId('retrain-model').fill('1')
  await page.getByTestId('retrain-fail').check()
  await page.getByTestId('retrain').click()
  await expect(page.getByTestId('retrain-message')).toContainText('保留最近完整预测结果')
  expect(await rows.allInnerTexts()).toEqual(before)
  await expect(page.getByTestId('active-batch')).toHaveText(beforeBatch)

  expect(errors).toEqual([])
})

test('指标定义 V019 读取预测留档的指标编码与精度位数', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto('/metric_def')
  const row = page.getByTestId('row-2')
  await expect(row).toBeVisible()
  await expect(row.getByTestId('forecast-batch')).toHaveText('FB-202609-11')
  await expect(row.getByTestId('check-METR-0002')).toHaveText('一致')

  // 按列表筛选条件缩小范围后仍按对应指标编码核对。
  await page.getByTestId('filter-指标编号').fill('METR-0003')
  await page.getByTestId('query').click()
  await expect(page.getByTestId('row-3').getByTestId('forecast-batch')).toHaveText('FB-202610-01')
})
