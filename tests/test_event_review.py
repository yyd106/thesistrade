"""Corporate-action rule v2. Announcements are synthetic, written in the two exchanges' public
layouts (fictional companies and numbers); no collected document is used."""
import argparse
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from ashare import governance, weekly
from ashare.event_review import (RULE_VERSION, cash_terms, check, dividend_dates, document_text, evaluate,
                                 follow_on, resolution, tight)
from ashare.storage import Store, normalize_time

SSE_CODE, SZSE_CODE = 'sh600001', 'sz000001'


def sse(per_share='0.81', record='2026/9/10', ex='2026/9/11', diff='否', row=None, extra=''):
    row = row or f'A股 {record} － {ex} {ex}'
    return f'''证券代码：600001 证券简称：示例股份 公告编号：临2026-060
示例股份有限公司2026年半年度权益分派实施公告
重要内容提示：
● 每股分配比例
A股每股现金红利{per_share}元
● 相关日期
股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日
{row}
● 差异化分红送转：{diff}
一、 通过分配方案的股东会届次和日期
本次利润分配方案经公司2026年8月24日的2026年第一次临时股东会审议通过。
二、 分配方案
本次利润分配以方案实施前的公司总股本3,000,000,000股为基数，每股派发现金红利{per_share}元（含税）。
三、 相关日期
股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日
{row}
四、 分配实施办法
（1）对于持有公司无限售条件流通股的个人股东，公司暂不扣缴个人所得税，每股实际派发现金红利人民币{per_share}元；
（2）对于合格境外机构投资者，按照10%的税率代扣代缴企业所得税，税后每股实际派发现金红利人民币0.729元。
{extra}
五、 有关咨询办法
关于本次权益分派事项如有疑问，请按以下联系方式咨询。'''


def szse(per_ten='13.300000', record='2026年9月7日', ex='2026年9月8日', extra='', base='以公司现有总股本1,200,000,000股为基数'):
    return f'''证券代码：000001 证券简称：示例科技 公告编号：2026-041
2026年第二次中期权益分派实施公告
二、本次实施的利润分配方案
本公司2026年第二次中期权益分派方案为：{base}，向全体股东每10股派{per_ten}元人民币现金（含税；扣税后，境外机构（含QFII、RQFII）以及持有首发前限售股的个人和证券投资基金每10股派11.970000元；持有首发后限售股、股权激励限售股及无限售流通股的个人股息红利税实行差别化税率征收）。
三、股权登记日与除权除息日
本次权益分派股权登记日为：{record}，除权除息日为：{ex}。
{extra}
四、权益分派对象
本次分派对象为：截止{record}下午深圳证券交易所收市后登记在册的本公司全体股东。'''


def red_chip(record='2026/9/29', ex='2026/9/30'):
    """A company listed in Hong Kong and Shanghai: the dividend is declared in HKD, A shares are paid
    in RMB, and the title says 利润分派 with "A 股" spaced out."""
    return f'''证券代码：600001 证券简称：示例通信 公告编号：2026-020
示例通信有限公司2026年中期利润分派 A 股实施公告
重要内容提示：
● 每股分配比例
A股每股现金红利人民币2.51元（含税）
● 相关日期
股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日
A股 {record} － {ex} {ex}
● 差异化分红送转：否
一、 通过分配方案的股东会届次和日期
本次利润分派方案经公司2026年8月6日的董事会审议通过。
二、 分配方案
本次利润分派以公司A股股数902,767,867股为基数，每股派发现金红利人民币2.51元（含税）。港元股息金额为每股2.9003港元，按照董事会宣派股息之日前一周港元对人民币中间价平均值计算。
港股股东（港股通股东除外）有权选择以港元或人民币可选择货币支付予港股股东，本公司以人民币支付予A股股东。
三、 相关日期
股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日
A股 {record} － {ex} {ex}
四、 分配实施办法
（1）对于持有本公司A股股份的个人股东，公司暂不扣缴个人所得税，每股实际派发现金红利人民币2.51元；
（2）对于合格境外机构投资者（QFII），按照10%的税率代扣代缴企业所得税，税后每股实际派发现金红利人民币2.259元。如 QFII 股东认为其取得的股息红利收入需要享受税收协定待遇的，可按照规定申请。
五、 有关咨询办法
关于本次利润分派事项如有疑问，请按以下联系方式咨询。'''


# How an SSE tax section describes investors holding the company's shares; not treasury shares.
TAX_SECTION = ('（1）对于持有本公司无限售条件流通股的个人股东及证券投资基金，持股期限超过1年的，股息红利所得暂免征收个人所得税，每股实际派发现金红利人民币0.81元；'
               '（2）对于持有本公司股票的合格境外机构投资者（QFII），由本公司按照10%的税率统一代扣代缴企业所得税，税后每股实际派发现金红利人民币0.729元；'
               '（3）对于通过沪股通持有本公司股份的香港市场投资者（包括企业和个人），其股息红利将由本公司通过中国结算上海分公司按股票名义持有人账户以人民币派发，扣税后每股实际派发现金红利人民币0.729元；'
               '（4）对于投资者所持本公司股份的其他情形，由纳税人按税法规定自行判断是否应在当地缴纳企业所得税。')
# A buyback price cap adjusted after the dividend, written as one formula.
CAP_FORMULA = '调整后的回购价格上限=调整前的回购价格上限-每股现金红利=56.55元/股-2.00元/股=54.55元/股。'

BUYBACK_CLAUSE = '若公司在回购期内发生派息、送股、资本公积金转增股本、股票拆细、缩股、配股等除权除息事项，自股价除权除息之日起，相应调整回购价格上限。'


class CashTermsTests(unittest.TestCase):
    def test_sse_table_and_per_share_amount_are_read(self):
        terms, problems = cash_terms(sse(), SSE_CODE)
        self.assertEqual(problems, [])
        self.assertEqual(terms, {'cash_per_share': '0.81', 'cash_per_share_cents': 81, 'record_date': '2026-09-10', 'ex_date': '2026-09-11'})

    def test_szse_sentence_and_per_ten_amount_are_read(self):
        terms, problems = cash_terms(szse(), SZSE_CODE)
        self.assertEqual(problems, [])
        self.assertEqual((terms['cash_per_share'], terms['cash_per_share_cents']), ('1.33', 133))
        self.assertEqual((terms['record_date'], terms['ex_date']), ('2026-09-07', '2026-09-08'))

    def test_sub_cent_amounts_stay_exact(self):
        for amount in ('0.055', '0.075'):
            terms, problems = cash_terms(sse(per_share=amount, record='2026/9/22', ex='2026/9/23'), SSE_CODE)
            self.assertEqual(problems, [])
            self.assertEqual((terms['cash_per_share'], terms['cash_per_share_cents']), (amount, None))
        terms, _ = cash_terms(szse(per_ten='3.126541'), SZSE_CODE)
        self.assertEqual(terms['cash_per_share'], '0.3126541')

    def test_table_dates_are_not_glued_to_the_next_date_or_a_page_number(self):
        # Removing all whitespace turned "2026/9/1 2026/9/1" (or "2026/9/1" + page number 2) into 2026/9/12.
        text = sse(row='A股 2026/8/31 － 2026/9/1 2026/9/1\n2')
        self.assertEqual(dividend_dates(tight(text)), ('2026-08-31', '2026-09-01'))
        self.assertEqual(dividend_dates(tight(sse(row='A股 2026/9/10 2026/9/11 2026/9/11'))), ('2026-09-10', '2026-09-11'))
        # Without the dash, three more dates are ambiguous; the table is not used.
        self.assertEqual(dividend_dates(tight(sse(row='A股 2026/9/10 2026/9/11 2026/9/11 2026/9/30'))), (None, None))

    def test_explicit_no_and_conditional_clauses_do_not_block(self):
        self.assertEqual(cash_terms(sse(extra=BUYBACK_CLAUSE), SSE_CODE)[1], [])
        self.assertEqual(cash_terms(szse(extra=BUYBACK_CLAUSE + '本次不送红股，不以资本公积金转增股本。'), SZSE_CODE)[1], [])
        self.assertEqual(cash_terms(szse().replace('以公司现有总股本', '以公司现有总股本剔除已回购股份0股后的'), SZSE_CODE)[1], [])

    def test_real_differentiated_stock_and_rights_distributions_stay_blocked(self):
        cases = {
            'SSE label': sse(diff='是'),
            'SZSE treasury': szse(extra='公司回购专用证券账户中的股份不参与本次权益分派。按公司总股本折算每10股现金分红金额=13.18元。'),
            'SZSE excluded shares': szse().replace('以公司现有总股本', '以公司现有总股本剔除已回购股份8,000,000股后的'),
            'unclear mention': sse(extra='本次差异化分红除权（息）的计算依据见附件。').replace('● 差异化分红送转：否', ''),
        }
        for name, text in cases.items():
            with self.subTest(name):
                problems = cash_terms(text, SSE_CODE if text.startswith('证券代码：600001') else SZSE_CODE)[1]
                self.assertTrue(any(k == 'UNSUPPORTED' and '差异化分红' in m for k, m in problems), problems)
        stock = cash_terms(szse(extra='以资本公积金向全体股东每10股转增4股。'), SZSE_CODE)[1]
        self.assertIn(('UNSUPPORTED', '本次分配包含送股或资本公积金转增；当前核验规则只支持纯现金分红，送转未支持'), stock)
        rights = cash_terms(szse(extra='本次配股价格为8.00元。'), SZSE_CODE)[1]
        self.assertIn(('UNSUPPORTED', '公告写有配股安排；当前核验规则未支持配股'), rights)

    def test_after_tax_amounts_are_ignored_and_conflicting_amounts_block(self):
        self.assertEqual(cash_terms(sse(), SSE_CODE)[0]['cash_per_share'], '0.81')  # 0.729 is after tax
        terms, problems = cash_terms(sse(extra='每股派发现金红利0.90元（含税）。'), SSE_CODE)
        self.assertIsNone(terms['cash_per_share'])
        self.assertTrue(any('0.81、0.9' in m for _, m in problems), problems)

    def test_dates_must_be_adjacent_trading_days(self):
        problems = cash_terms(sse(ex='2026/9/14'), SSE_CODE)[1]
        self.assertTrue(any('登记日后的第一个交易日' in m for _, m in problems), problems)
        # 2026-09-25 is a market holiday: the Monday after it is the next trading day.
        self.assertEqual(cash_terms(sse(record='2026/9/24', ex='2026/9/28'), SSE_CODE)[1], [])

    def test_wrong_stock_is_never_verified(self):
        problems = cash_terms(sse(), 'sh600002')[1]
        self.assertIn(('EVIDENCE', '公告正文中的证券代码须与本股票一致'), problems)

    def test_treasury_wordings_count_as_differentiated(self):
        base = '以公司现有总股本1,250,000,000股{}后的1,200,000,000股为基数'
        texts = [szse(base=base.format(w)) for w in ('扣除回购专用证券账户中已回购股份50,000,000股', '剔除已回购股份', '减去公司回购账户持有的50,000,000股')]
        texts += [szse(base='以公司现有总股本（已剔除回购股份50,000,000股）1,200,000,000股为基数'),
                  szse(base='以公司总股本1,250,000,000股中的1,200,000,000股为基数')]
        texts += [szse(extra=x) for x in ('截至本公告披露日，公司通过回购专用证券账户累计持有公司股份110,389,527股，上述股份不参与本次利润分配。',
                                          '公司持有的库存股50,000,000股不参与本次权益分派。',
                                          '本次权益分派实施后，按公司A股总股本折算，每股现金红利为1.2768元。',
                                          '根据股票市值不变原则，现金分红总额分摊到每一股的比例将减小。',
                                          '本次权益分派实施后除权除息价格计算时，每股现金红利应以1.2768元/股计算。')]
        for text in texts:
            with self.subTest(text[-60:]):
                self.assertTrue(any(k == 'UNSUPPORTED' and ('差异化' in m) for k, m in cash_terms(text, SZSE_CODE)[1]))

    def test_investors_holding_shares_are_not_treasury_shares(self):
        self.assertEqual(cash_terms(sse(extra=TAX_SECTION), SSE_CODE)[1], [])
        held = cash_terms(sse(extra='公司持有的本公司股份30,000,000股不参与本次利润分配。'), SSE_CODE)[1]
        self.assertTrue(any('差异化' in m for _, m in held))
        self.assertTrue(any('差异化' in m for _, m in cash_terms(sse(extra='公司通过回购专用证券账户所持本公司股份30,000,000股。'), SSE_CODE)[1]))

    def test_a_price_cap_formula_is_not_a_cash_amount(self):
        per_ten = szse(per_ten='20.000000', extra='公司将相应调整回购股份价格上限。' + CAP_FORMULA)
        self.assertEqual(cash_terms(per_ten, SZSE_CODE)[1], [])
        self.assertTrue(any('1.2768' in m for _, m in cash_terms(szse(extra='每股现金红利1.2768元/股。'), SZSE_CODE)[1]))

    def test_partly_cancelled_treasury_still_counts(self):
        for extra in ('截至本公告披露日，公司回购专用证券账户中的股份已注销20,000,000股，剩余30,000,000股不参与本次权益分派。',
                      '公司前期回购股份已全部注销后又新增回购30,000,000股，该等股份不参与本次权益分派。',
                      '公司所持本公司股份30,000,000股不享有本次分配。'):
            with self.subTest(extra):
                self.assertTrue(any('差异化' in m for _, m in cash_terms(szse(extra=extra), SZSE_CODE)[1]))

    def test_stock_distribution_wordings_block_even_with_a_conditional_tail(self):
        extras = ('同时，以资本公积金向全体股东每10股转增1.000000股，分配方案披露至实施期间公司股本总额如发生变化，将按照分配比例不变的原则相应调整。',
                  '本次权益分派方案为每10股派2元送3股。', '向全体股东每10股派送2股。', '向全体股东每十股送红股二股。',
                  '分红前本公司总股本为1,000,000,000股，分红后总股本增至1,100,000,000股。')
        for extra in extras:
            with self.subTest(extra):
                self.assertTrue(any('送转未支持' in m for _, m in cash_terms(szse(extra=extra), SZSE_CODE)[1]))
        self.assertTrue(any('配股' in m for _, m in cash_terms(szse(extra='本次配股按每10股配售3股的比例向全体股东配售。'), SZSE_CODE)[1]))

    def test_a_second_cash_amount_blocks(self):
        problems = cash_terms(szse(extra='另向全体股东每10股派发特别现金红利5.000000元人民币（含税）。'), SZSE_CODE)[1]
        self.assertTrue(any('其他每股派现金额（0.5 元）' in m for _, m in problems), problems)
        # After-tax figures in the same sentence are skipped only up to the end of their parenthesis.
        text = szse().replace('个人股息红利税实行差别化税率征收）。', '个人股息红利税实行差别化税率征收），另向全体股东每10股发放特别现金红利5元（含税）。')
        self.assertTrue(any('0.5 元' in m for _, m in cash_terms(text, SZSE_CODE)[1]))

    def test_a_h_announcements_declared_in_hong_kong_dollars_pass(self):
        head = sse(per_share='2.36').replace('本次利润分配以方案实施前的公司总股本3,000,000,000股为基数，每股派发现金红利2.36元（含税）。', '{}')
        for line in ('本公司2026年中期股息为每股派发港币2.60元（含税），A股股东以人民币派发，每股派发现金红利人民币2.36元（含税）。',
                     '本公司2026年中期股息为每股2.60港元（含税），按照宣派日前五个工作日港元兑人民币的平均基准汇率折算后每股派发现金红利人民币2.36元（含税）。',
                     '本次A股股息每股派发现金红利人民币2.36元（含税）。H股股东的股息以外币（港币）支付，不适用本公告。'):
            with self.subTest(line):
                self.assertEqual(cash_terms(head.format(line), SSE_CODE)[1], [])

    def test_other_share_classes_old_years_and_pdf_glyphs(self):
        self.assertEqual(cash_terms(sse(extra='H股股东的股权登记日为2026年8月28日，其现金红利派发不适用本公告。'), SSE_CODE)[1], [])
        self.assertEqual(cash_terms(szse(record='2025年9月8日', ex='2025年9月9日'), SZSE_CODE)[1], [])  # before the stored calendar
        self.assertTrue(cash_terms(szse(record='2025年9月8日', ex='2025年9月28日'), SZSE_CODE)[1])
        radicals = szse().replace('2026年9月7日', '2026年9月7\u2f47').replace('2026年9月8日', '2026年9月8\u2f47')
        self.assertEqual(cash_terms(radicals, SZSE_CODE)[0]['ex_date'], '2026-09-08')
        self.assertEqual(cash_terms(szse().replace('证券代码', '股票代码'), SZSE_CODE)[1], [])

    def test_follow_on_titles(self):
        self.assertTrue(follow_on('关于2026年半年度权益分派实施后调整回购股份价格上限的公告'))
        self.assertTrue(follow_on('关于实施权益分派时转股连续停牌的提示性公告'))
        self.assertFalse(follow_on('2026年半年度权益分派实施公告'))
        self.assertFalse(follow_on('关于调整回购股份价格上限的公告'))  # not tied to a dividend
        self.assertFalse(follow_on('关于取消2026年半年度权益分派并恢复转股的公告'))  # a cancellation is not a follow-on
        self.assertTrue(follow_on('2026年半年度权益分派实施公告（英文版）'))

    def test_profit_distribution_implementations_are_corporate_actions_but_plans_are_not(self):
        from ashare.materiality import classify
        for title in ('2025年年度利润分配实施公告', '关于2025年度利润分配方案实施的公告', '2026年半年度利润分配及资本公积金转增股本实施公告',
                      '2026年半年度现金红利派发实施公告', '2025年末期A股股息派发实施公告', '关于终止实施2026年半年度利润分配的公告',
                      '关于延期实施2026年半年度利润分配方案的公告'):
            self.assertEqual(classify(title, 'company_report', SSE_CODE)['level'], 'CORPORATE_ACTION', title)
        self.assertNotEqual(classify('关于2026年半年度利润分配预案的公告', 'company_report', SSE_CODE)['level'], 'CORPORATE_ACTION')
        self.assertNotEqual(classify('境内优先股股息派发实施公告', 'company_report', SSE_CODE)['level'], 'CORPORATE_ACTION')

    def test_resolution_of_reviews_written_before_v2_follows_their_wording(self):
        self.assertEqual(resolution({'status': 'NEEDS_EVIDENCE', 'missing': ['存在差异化、配股、外币或取消安排，需补充对应处理规则']}), 'UNSUPPORTED')
        self.assertEqual(resolution({'status': 'NEEDS_EVIDENCE', 'missing': ['除息日尚未到达，不提前确认未来实施结果']}), 'WAIT')
        self.assertEqual(resolution({'status': 'NEEDS_EVIDENCE', 'missing': ['需要最终实施公告']}), 'EVIDENCE')
        self.assertEqual(resolution({'status': 'NEEDS_EVIDENCE', 'missing': ['x'], 'resolution': 'LINKED'}), 'LINKED')
        self.assertIsNone(resolution({'status': 'VERIFIED', 'missing': []}))


class TitleCoverageTests(unittest.TestCase):
    def test_every_implementation_wording_is_a_corporate_action(self):
        from ashare.materiality import classify
        for title in ('2026年中期利润分派A股实施公告', '2026年中期利润分派 A 股实施公告', '2025年末期A股派息实施公告',
                      '关于2025年度现金红利发放的实施公告', '2025年年度分红实施公告', '2026年半年度权益分派实施公告',
                      '2025年末期A股股息派发实施公告', '关于暂缓实施2026年中期利润分派的公告', '关于2025年度分红实施公告的更正公告',
                      '关于2025年度利润分配实施后调整可转债转股价格的公告'):  # as before 0.15.5
            self.assertEqual(classify(title)['level'], 'CORPORATE_ACTION', title)
        # Not the company's own A-share implementation: none may start a block that never clears.
        for title in ('关于2025年度利润分配预案的公告', '未来三年股东分红回报规划（2026-2028年）', '关于H股末期股息派发的公告',
                      '关于回购股份实施结果的公告', '关于2025年年度分红实施后调整股权激励行权价格的公告', '关于全资子公司分红实施完毕的公告',
                      '关于H股2025年末期派息实施的公告', '2026年中期利润分派A股实施公告（英文版）', '2026年中期利润分派A股实施公告（English Version）',
                      '关于2026年中期利润分派实施后调整回购股份价格上限的公告', '关于2025年度分红实施完成的公告', '关于2025年度利润分派实施进展的公告',
                      '2025年度分红实施情况的说明', '利润分派实施完毕的公告', '关于派息实施之后调整发行价格的公告', '关于调整2025年度分红实施方案的公告',
                      '关于2025年度分红实施完成后调整回购股份价格上限的公告', '关于下属公司分红实施的公告'):
            self.assertNotEqual(classify(title)['level'], 'CORPORATE_ACTION', title)

    def test_red_chip_title_is_final_and_names_its_period(self):
        from ashare.event_review import FINAL, period
        self.assertTrue(FINAL.search(tight('2026年中期利润分派 A 股实施公告')))
        self.assertEqual(period('2026年中期利润分派 A 股实施公告'), '2026年半年度')
        self.assertEqual(period('2025年末期A股派息实施公告'), '2025年度')

    def test_a_halt_reaches_every_non_annual_distribution_of_its_year(self):
        from ashare.event_review import same_distribution as same
        for halt, main in (('关于暂缓实施2026年中期分红的公告', '2026年第一次中期权益分派实施公告'),
                           ('关于延期实施2026年半年度分红的公告', '2026年第二次中期权益分派实施公告'),
                           ('关于暂缓实施2026年度中期分红的公告', '2026年半年度权益分派实施公告'),
                           ('关于暂缓实施2026年第1次中期分红的公告', '2026年第一次中期权益分派实施公告'),
                           ('关于暂缓实施2026年首次中期分红的公告', '2026年第一次中期权益分派实施公告'),
                           ('关于暂缓实施2025年第二次中期分红的公告', '2025年前三季度权益分派实施公告'),
                           ('关于暂缓实施本次权益分派的公告', '2026年第一次中期权益分派实施公告'),
                           ('关于暂缓实施2025年度利润分配的公告', '2025年年度权益分派实施公告')):
            self.assertTrue(same(halt, main, halt=True), halt)
        for halt, main in (('关于终止实施2024年度利润分配的公告', '2026年半年度权益分派实施公告'),
                           ('关于暂缓实施2026年度利润分配的公告', '2026年半年度权益分派实施公告')):
            self.assertFalse(same(halt, main, halt=True), halt)
        # Two implementations keep exact periods: two interim dividends of a year are two distributions.
        self.assertFalse(same('2026年第一次中期权益分派实施公告', '2026年半年度权益分派实施公告'))
        self.assertFalse(same('2026年第一次中期权益分派实施公告', '2026年第二次中期权益分派实施公告'))
        self.assertTrue(same('2026年度中期利润分配实施公告', '2026年半年度权益分派实施公告'))

    def test_red_chip_announcement_reads_rmb_amount_and_ignores_hkd_and_after_tax(self):
        terms, problems = cash_terms(red_chip(), SSE_CODE)
        self.assertEqual(problems, [])
        self.assertEqual(terms, {'cash_per_share': '2.51', 'cash_per_share_cents': 251, 'record_date': '2026-09-29', 'ex_date': '2026-09-30'})


def bars(first, last, close='10'):
    day, out = date.fromisoformat(first), []
    while day.isoformat() <= last:
        out.append([day.isoformat(), close, close, close, close]);day += timedelta(days=1)
    return out


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def doc(self, doc_id, text, title='2026年半年度权益分派实施公告', symbol=SZSE_CODE, stamp='2026-09-01T09:00:00+08:00'):
        """Stored through add_document, so the chunks overlap exactly as collected ones do."""
        did, _ = self.store.add_document(symbol=symbol, kind='company_report', title=title, source='cninfo',
                                         url='https://static.cninfo.com.cn/' + doc_id + '.pdf', published_at=stamp, first_seen_at=stamp,
                                         ready_at=stamp, pages=[(1, text)], raw_path='fixture', cloud_allowed=True)
        row = dict(self.store.db.execute('SELECT * FROM documents WHERE id=?', (did,)).fetchone())
        return row, [dict(c) for c in self.store.db.execute('SELECT * FROM chunks WHERE doc_id=? ORDER BY ordinal', (did,))]

    def features(self, first='2026-07-01', last='2026-09-17'):
        b = bars(first, last)
        return {'unadjusted': {'bars': b, 'ma20_cents': 1000, 'ma60_cents': 1000, 'close_cents': 1000,
                               'basis': 'UNADJUSTED', 'last_complete_date': b[-1][0]}}

    def test_sub_cent_dividend_adjusts_pre_ex_closes_exactly(self):
        doc, chunks = self.doc('a', szse(per_ten='0.550000', record='2026年8月18日', ex='2026年8月19日'))
        reviews, u = evaluate(self.store, SZSE_CODE, [doc], {doc['id']: chunks}, self.features(), normalize_time('2026-09-18T10:00:00+08:00'))
        self.assertEqual((reviews[0]['status'], reviews[0]['rule_version']), ('VERIFIED', RULE_VERSION))
        self.assertEqual(reviews[0]['facts']['cash_per_share'], '0.055')
        self.assertEqual(u['basis'], 'CASH_DIVIDEND_ADJUSTED')
        self.assertEqual((u['bars'][0][2], u['bars'][-1][2]), ('9.945', '10'))
        # Last 60 bars: 30 after the ex-date at 1000 and 30 before at 994.5 cents -> 997.25, floored.
        self.assertEqual((u['ma20_cents'], u['ma60_cents'], u['close_cents']), (1000, 997, 1000))
        json.dumps(u);json.dumps(reviews)  # everything stays serialisable

    def test_page_is_rebuilt_across_chunk_edges(self):
        filler = '本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏。'
        text = sse().replace('一、 通过分配方案', filler * 30 + '一、 通过分配方案')
        doc, chunks = self.doc('b', text, symbol=SSE_CODE)
        self.assertGreater(len(chunks), 2)
        self.assertEqual(document_text(chunks), text.strip())  # the 80-character overlaps are not repeated
        self.assertEqual(cash_terms(document_text(chunks), SSE_CODE)[1], [])
        pages = [{'id': 'p1', 'page': 1, 'ordinal': 0, 'text': '甲' * 800}, {'id': 'p2', 'page': 1, 'ordinal': 1, 'text': '甲' * 80 + '乙'},
                 {'id': 'p3', 'page': 2, 'ordinal': 2, 'text': '丙'}]
        self.assertEqual(document_text(pages), '甲' * 800 + '乙' + '\n' + '丙')
        for length in (1440, 1441, 1479, 1500, 1520, 2300):  # the last window can lie inside the previous one
            page = ''.join(chr(0x4e00 + i % 500) for i in range(length))
            windows = [{'id': str(i), 'page': 1, 'ordinal': i, 'text': page[o:o + 800]} for i, o in enumerate(range(0, length, 720))]
            self.assertEqual(document_text(windows), page, length)

    def test_future_dividend_waits_without_asking_for_bars(self):
        doc, chunks = self.doc('c', szse(record='2026年9月28日', ex='2026年9月29日'))
        reviews, _ = evaluate(self.store, SZSE_CODE, [doc], {doc['id']: chunks}, self.features(last='2026-09-24'),
                              normalize_time('2026-09-26T10:00:00+08:00'))
        self.assertEqual((reviews[0]['status'], reviews[0]['resolution']), ('NEEDS_EVIDENCE', 'WAIT'))
        self.assertNotIn('日线中尚无除息日记录', str(reviews[0]['missing']))

    def test_follow_on_announcement_takes_the_state_of_its_dividend(self):
        main, main_chunks = self.doc('d', szse(record='2026年8月18日', ex='2026年8月19日'))
        text = ('证券代码：000001 证券简称：示例科技 公司2026年半年度权益分派的股权登记日为2026年8月18日，除权除息日为2026年8月19日。'
                '根据回购方案，自除权除息之日起调整回购股份价格上限。' + BUYBACK_CLAUSE)
        follow, follow_chunks = self.doc('e', text, title='关于2026年半年度权益分派实施后调整回购股份价格上限的公告')
        chunks = {main['id']: main_chunks, follow['id']: follow_chunks}
        at = normalize_time('2026-09-18T10:00:00+08:00')
        reviews, u = evaluate(self.store, SZSE_CODE, [main, follow], chunks, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['VERIFIED', 'VERIFIED'])
        self.assertEqual(reviews[1]['facts']['linked_doc_id'], main['id'])
        self.assertEqual(len(u['corporate_actions']), 1)  # the price is adjusted once
        # When the dividend itself is unsupported, the follow-on waits for it instead of asking for more text.
        diff, diff_chunks = self.doc('f', szse(record='2026年8月18日', ex='2026年8月19日', extra='公司回购专用证券账户中的股份不参与本次权益分派。'))
        reviews, u = evaluate(self.store, SZSE_CODE, [diff, follow], {diff['id']: diff_chunks, follow['id']: follow_chunks}, self.features(), at)
        self.assertEqual([(r['status'], r['resolution']) for r in reviews], [('NEEDS_EVIDENCE', 'UNSUPPORTED'), ('NEEDS_EVIDENCE', 'LINKED')])
        self.assertEqual(u['basis'], 'UNADJUSTED')
        # Alone it stays blocked with the reason.
        reviews, _ = evaluate(self.store, SZSE_CODE, [follow], {follow['id']: follow_chunks}, self.features(), at)
        self.assertEqual(reviews[0]['resolution'], 'EVIDENCE')
        # Without dates it links to the one implementation published within 15 days; otherwise it stays blocked.
        text = '证券代码：000001 因实施权益分派，示例转债自2026年8月12日起至本次权益分派股权登记日止暂停转股。'
        bare, bare_chunks = self.doc('g', text, title='关于实施2026年半年度权益分派期间示例转债暂停转股的公告')
        reviews, _ = evaluate(self.store, SZSE_CODE, [main, bare], {main['id']: main_chunks, bare['id']: bare_chunks}, self.features(), at)
        self.assertEqual((reviews[1]['status'], reviews[1]['facts']['linked_doc_id']), ('VERIFIED', main['id']))
        late, late_chunks = self.doc('h', text, title='关于实施2026年半年度权益分派期间示例转债暂停转股的公告', stamp='2026-10-20T09:00:00+08:00')
        reviews, _ = evaluate(self.store, SZSE_CODE, [main, late], {main['id']: main_chunks, late['id']: late_chunks}, self.features(), at)
        self.assertEqual(reviews[1]['resolution'], 'UNSUPPORTED')

    def test_price_cap_formula_in_a_follow_on_is_not_read_as_cash(self):
        at = normalize_time('2026-09-18T10:00:00+08:00')
        main, c1 = self.doc('x', szse(per_ten='20.000000', record='2026年8月18日', ex='2026年8月19日'))
        text = '证券代码：000001 本次权益分派股权登记日为2026年8月18日，除权除息日为2026年8月19日。' + CAP_FORMULA
        follow, c2 = self.doc('y', text, title='关于2026年半年度权益分派实施后调整股份回购价格上限的公告')
        reviews, _ = evaluate(self.store, SZSE_CODE, [main, follow], {main['id']: c1, follow['id']: c2}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['VERIFIED'] * 2)
        held, c3 = self.doc('z', text.replace('。调整', '。截至本公告披露日，公司通过回购专用证券账户累计回购股份495,600股。调整'),
                            title='关于2026年半年度权益分派实施后调整股份回购价格上限的公告')
        reviews, _ = evaluate(self.store, SZSE_CODE, [main, held], {main['id']: c1, held['id']: c3}, self.features(), at)
        self.assertEqual([r['resolution'] for r in reviews], ['UNSUPPORTED'] * 2)
        self.assertNotIn('56.55', json.dumps(reviews, ensure_ascii=False))  # blocked for the treasury shares only

    def test_buyback_notices_without_holdings_follow_their_dividend(self):
        at = normalize_time('2026-09-18T10:00:00+08:00')
        main, c1 = self.doc('u', szse(record='2026年8月18日', ex='2026年8月19日'))
        head = '证券代码：000001 本次权益分派股权登记日为2026年8月18日，除权除息日为2026年8月19日。'
        tail = '若公司在回购期内实施派息，自股价除权除息之日起相应调整回购价格上限。调整后的回购价格上限=50.00-1.33=48.67元/股。'
        for i, body in enumerate(('公司拟回购公司股份，回购的股份将用于员工持股计划，回购价格不超过50.00元/股。截至本公告披露日，公司尚未实施回购。',
                                  '截至本公告披露日，公司通过回购专用证券账户以集中竞价方式累计回购股份0股。')):
            follow, c2 = self.doc('v' + str(i), head + body + tail, title='关于2026年半年度权益分派实施后调整回购股份价格上限的公告')
            reviews, _ = evaluate(self.store, SZSE_CODE, [main, follow], {main['id']: c1, follow['id']: c2}, self.features(), at)
            self.assertEqual([r['status'] for r in reviews], ['VERIFIED'] * 2, body)
        held, c3 = self.doc('w', head + '截至本公告披露日，公司通过回购专用证券账户累计回购股份30,000,000股。' + tail,
                            title='关于2026年半年度权益分派实施后调整回购股份价格上限的公告')
        reviews, _ = evaluate(self.store, SZSE_CODE, [main, held], {main['id']: c1, held['id']: c3}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['NEEDS_EVIDENCE'] * 2)

    def test_follow_on_text_or_a_cancellation_blocks_its_dividend(self):
        at = normalize_time('2026-09-18T10:00:00+08:00')
        main, main_chunks = self.doc('i', szse(record='2026年8月18日', ex='2026年8月19日'))
        text = ('证券代码：000001 公司2026年半年度权益分派股权登记日为2026年8月18日，除权除息日为2026年8月19日。'
                '由于公司回购专用证券账户中的股份不参与本次权益分派，按公司总股本折算每股现金红利为1.2768元。')
        follow, follow_chunks = self.doc('j', text, title='关于2026年半年度权益分派实施后调整回购股份价格上限的公告')
        reviews, u = evaluate(self.store, SZSE_CODE, [main, follow], {main['id']: main_chunks, follow['id']: follow_chunks}, self.features(), at)
        self.assertEqual([(r['status'], r['resolution']) for r in reviews], [('NEEDS_EVIDENCE', 'UNSUPPORTED')] * 2)
        self.assertIn('随附公告', reviews[0]['missing'][-1])
        self.assertEqual(u['basis'], 'UNADJUSTED')
        cancel, cancel_chunks = self.doc('k', '证券代码：000001 公司决定取消本次权益分派，示例转债恢复转股。', title='关于取消2026年半年度权益分派并恢复转股的公告')
        reviews, u = evaluate(self.store, SZSE_CODE, [main, cancel], {main['id']: main_chunks, cancel['id']: cancel_chunks}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['NEEDS_EVIDENCE'] * 2)
        self.assertEqual(u['basis'], 'UNADJUSTED')

    def test_revisions_and_repeats_of_one_distribution_never_adjust_twice(self):
        at = normalize_time('2026-09-18T10:00:00+08:00')
        first, c1 = self.doc('l', szse(record='2026年8月18日', ex='2026年8月19日'))
        supplement, c2 = self.doc('m', szse(record='2026年8月19日', ex='2026年8月20日'), title='关于2026年半年度权益分派实施公告的补充公告')
        reviews, u = evaluate(self.store, SZSE_CODE, [first, supplement], {first['id']: c1, supplement['id']: c2}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['NEEDS_EVIDENCE'] * 2)
        self.assertEqual(u['basis'], 'UNADJUSTED')
        again, c3 = self.doc('n', szse(record='2026年8月19日', ex='2026年8月20日'), title='2026年半年度权益分派实施公告（再次披露）')
        reviews, u = evaluate(self.store, SZSE_CODE, [first, again], {first['id']: c1, again['id']: c3}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['NEEDS_EVIDENCE'] * 2)
        other, c4 = self.doc('o', szse(record='2026年7月7日', ex='2026年7月8日'), title='2026年第一季度权益分派实施公告')
        reviews, u = evaluate(self.store, SZSE_CODE, [first, other], {first['id']: c1, other['id']: c4}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['VERIFIED'] * 2)  # two distributions, two adjustments
        self.assertEqual(len(u['corporate_actions']), 2)

    def test_follow_on_amount_and_period_spellings(self):
        at = normalize_time('2026-09-18T10:00:00+08:00')
        main, c1 = self.doc('q', szse(record='2026年8月18日', ex='2026年8月19日'))
        cap, c2 = self.doc('r', '证券代码：000001 本次权益分派股权登记日为2026年8月18日，除权除息日为2026年8月19日。'
                                '调整后的回购价格上限=调整前的回购价格上限50.00元/股-每股现金红利1.2768元/股=48.72元/股。',
                           title='关于2026年半年度权益分派实施后调整回购股份价格上限的公告')
        reviews, u = evaluate(self.store, SZSE_CODE, [main, cap], {main['id']: c1, cap['id']: c2}, self.features(), at)
        self.assertEqual([r['status'] for r in reviews], ['NEEDS_EVIDENCE'] * 2)
        self.assertIn('1.2768', reviews[0]['missing'][-1])
        # The same distribution spelled differently, or a title naming no period, is still the same distribution.
        annual, c3 = self.doc('s', szse(record='2026年8月18日', ex='2026年8月19日'), title='2025年年度权益分派实施公告')
        for title in ('关于2025年度权益分派实施公告的更正公告', '2025年度权益分派实施公告（修正版）', '关于暂缓实施2025年度利润分配的公告'):
            other, c4 = self.doc('t' + title, szse(record='2026年8月19日', ex='2026年8月20日'), title=title)
            reviews, u = evaluate(self.store, SZSE_CODE, [annual, other], {annual['id']: c3, other['id']: c4}, self.features(), at)
            self.assertEqual((reviews[0]['status'], u['basis']), ('NEEDS_EVIDENCE', 'UNADJUSTED'), title)

    def test_exchange_reference_price_backs_up_the_amount(self):
        doc, chunks = self.doc('p', szse(record='2026年8月18日', ex='2026年8月19日'))
        at = normalize_time('2026-09-18T10:00:00+08:00')
        def run(prev_close):
            with self.store.db:
                self.store.db.execute('DELETE FROM quotes')
                self.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)', ('q', SZSE_CODE, '示例', 900, prev_close,
                                      normalize_time('2026-08-19T10:00:00+08:00'), normalize_time('2026-08-19T10:00:05+08:00'), 'test', 'x'))
            return evaluate(self.store, SZSE_CODE, [doc], {doc['id']: chunks}, self.features(), at)[0][0]
        self.assertEqual(run(867)['status'], 'VERIFIED')   # 10.00 - 1.33
        self.assertEqual(run(1000)['status'], 'VERIFIED')  # the source did not adjust: no evidence either way
        for reference in (700, 868):                       # a bonus issue the text did not show; a smaller virtual amount
            blocked = run(reference)
            self.assertEqual((blocked['status'], blocked['resolution']), ('NEEDS_EVIDENCE', 'UNSUPPORTED'), reference)
            self.assertIn('参考价', blocked['missing'][0])


    def test_shares_held_at_the_record_date_need_a_ledger_that_credits_the_dividend(self):
        from unittest.mock import patch
        doc, chunks = self.doc('rc', red_chip(), title='2026年中期利润分派 A 股实施公告', symbol=SSE_CODE)
        at, features = normalize_time('2026-09-30T20:00:00+08:00'), self.features(last='2026-09-30')
        self.assertEqual(evaluate(self.store, SSE_CODE, [doc], {doc['id']: chunks}, features, at)[0][0]['status'], 'VERIFIED')
        with patch('ashare.event_review.held_at_record', return_value=200):
            blocked = evaluate(self.store, SSE_CODE, [doc], {doc['id']: chunks}, features, at)[0][0]
            self.assertEqual(blocked['resolution'], 'UNSUPPORTED')
            self.assertIn('登记日持有', blocked['missing'][0])
            reviews, u = evaluate(self.store, SSE_CODE, [doc], {doc['id']: chunks}, features, at, credits=True)
        self.assertEqual(reviews[0]['status'], 'VERIFIED')
        self.assertEqual([(a['ex_date'], a['cash_per_share']) for a in u['corporate_actions']], [('2026-09-30', '2.51')])
        # Before the ex-date it waits either way.
        waiting = evaluate(self.store, SSE_CODE, [doc], {doc['id']: chunks}, self.features(last='2026-09-28'),
                           normalize_time('2026-09-28T20:00:00+08:00'), credits=True)[0][0]
        self.assertEqual(waiting['resolution'], 'WAIT')

    def test_postponing_an_interim_dividend_voids_a_numbered_interim_implementation(self):
        main, c1 = self.doc('np', szse(record='2026年9月7日', ex='2026年9月8日'), title='2026年第一次中期权益分派实施公告')
        at = normalize_time('2026-09-18T10:00:00+08:00')
        self.assertEqual(evaluate(self.store, SZSE_CODE, [main], {main['id']: c1}, self.features(), at)[0][0]['status'], 'VERIFIED')
        for title in ('关于暂缓实施2026年中期分红的公告', '关于延期实施2026年半年度分红的公告'):
            halt, c2 = self.doc('h' + title, '证券代码：000001 公司决定暂缓实施本次分红。', title=title, stamp='2026-09-04T09:00:00+08:00')
            reviews, u = evaluate(self.store, SZSE_CODE, [main, halt], {main['id']: c1, halt['id']: c2}, self.features(), at, credits=True)
            self.assertEqual((reviews[0]['status'], u['basis']), ('NEEDS_EVIDENCE', 'UNADJUSTED'), title)
            self.assertNotIn('corporate_actions', u)

    def test_two_interim_dividends_of_one_year_are_both_verified(self):
        first, c1 = self.doc('i1', szse(record='2026年6月16日', ex='2026年6月17日'), title='2026年第一次中期权益分派实施公告',
                             stamp='2026-06-10T09:00:00+08:00')
        second, c2 = self.doc('i2', szse(record='2026年9月7日', ex='2026年9月8日'), title='2026年半年度权益分派实施公告')
        reviews, u = evaluate(self.store, SZSE_CODE, [first, second], {first['id']: c1, second['id']: c2},
                              self.features(first='2026-05-01'), normalize_time('2026-09-18T10:00:00+08:00'))
        self.assertEqual([r['status'] for r in reviews], ['VERIFIED', 'VERIFIED'])
        self.assertEqual([a['ex_date'] for a in u['corporate_actions']], ['2026-06-17', '2026-09-08'])

    def test_replay_lists_unrecognised_payout_titles_and_the_cash_to_credit(self):
        from unittest.mock import patch
        from test_config import load_config
        from ashare.demo import seed, SYMBOL
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        cfg.update(data_dir=self.tmp.name, watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}])
        seed(self.store, cfg)
        text = red_chip(record='2026/9/10', ex='2026/9/11').replace('600001', SYMBOL[2:])
        for n, title in enumerate(('2026年中期利润分派 A 股实施公告', '关于派发2026年中期现金红利的公告', '关于2025年度利润分配预案的公告')):
            self.store.add_document(symbol=SYMBOL, kind='company_report', title=title, source='cninfo',
                                    url=f'https://static.cninfo.com.cn/u{n}.pdf', published_at='2026-09-01T09:00:00+08:00',
                                    first_seen_at='2026-09-01T09:00:00+08:00', ready_at='2026-09-01T09:00:00+08:00',
                                    pages=[(1, text if n == 0 else '证券代码：' + SYMBOL[2:] + ' 公告内容')], raw_path='fixture', cloud_allowed=True)
        with patch('ashare.event_review.held_at_record', return_value=200):
            report = check(self.store, cfg, at='2026-09-15T10:00:00+08:00')
        stock = report['stocks'][0]
        self.assertTrue(report['dividend_credit'])  # a standalone install credits dividends itself
        self.assertEqual([u['title'] for u in stock['unrecognised']], ['关于派发2026年中期现金红利的公告'])
        self.assertEqual(report['summary']['unrecognised_titles'], 1)
        self.assertEqual(stock['reviews'][0]['held_at_record'], {'qty': 200, 'cash_cents': 50200})
        cfg['deployment_role'] = 'research'  # a research node waits for the cloud to advertise it
        self.assertFalse(check(self.store, cfg, at='2026-09-15T10:00:00+08:00')['dividend_credit'])

    def test_replay_reports_blocking_actions_without_document_text(self):
        from test_config import load_config
        from ashare.demo import seed, SYMBOL
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        cfg.update(data_dir=self.tmp.name, watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}])
        seed(self.store, cfg)
        text = szse(extra='公司回购专用证券账户中的股份不参与本次权益分派。').replace('000001', SYMBOL[2:])
        self.store.add_document(symbol=SYMBOL, kind='company_report', title='2026年半年度权益分派实施公告', source='cninfo',
                                url='https://static.cninfo.com.cn/h.pdf', published_at='2026-09-01T09:00:00+08:00',
                                first_seen_at='2026-09-01T09:00:00+08:00', ready_at='2026-09-01T09:00:00+08:00',
                                pages=[(1, text)], raw_path='fixture', cloud_allowed=True)
        report = check(self.store, cfg, at='2026-09-15T10:00:00+08:00')
        stock = report['stocks'][0]
        self.assertTrue(stock['blocks_buying'])
        self.assertEqual((stock['reviews'][0]['resolution'], report['summary']['blocked_by_corporate_actions']), ('UNSUPPORTED', 1))
        self.assertNotIn('截止', json.dumps(report, ensure_ascii=False))  # reasons and facts only
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM snapshots').fetchone()[0], 1)  # only seed's
        self.assertIn('不在自选股内', check(self.store, cfg, ['000333'], at='2026-09-15T10:00:00+08:00')['stocks'][0]['error'])


class GovernanceToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def draft(self, title):
        with self.store.db:
            return governance.draft_proposal(self.store, source='agent', kind='RULE', target='自选股', title=title,
                                             payload={'hypothesis': 'h'}, at='2026-09-26T01:00:00+00:00')

    def test_superseded_proposal_names_its_replacement_and_keeps_history(self):
        old, new = self.draft('旧版'), self.draft('完整新版')
        governance.decide(self.store, old, 'REJECTED', decided_by=None, note='被完整新版替代', at='2026-09-26T01:10:00+00:00')
        for bad, message in ((old, '不能是它自己'), ('missing', '未找到新版提案'), (None, '--replaced-by')):
            with self.assertRaisesRegex(ValueError, message):
                governance.decide(self.store, old, 'SUPERSEDED', decided_by=None, note='改标', replaced_by=bad)
        governance.decide(self.store, old, 'SUPERSEDED', decided_by=None, note='改为被新版替代', replaced_by=new, at='2026-09-26T02:00:00+00:00')
        rows = {p['id']: p for p in governance.proposals(self.store)}
        self.assertEqual((rows[old]['status'], rows[old]['payload']['superseded_by']), ('SUPERSEDED', new))
        self.assertEqual([h['to'] for h in rows[old]['payload']['history']], ['REJECTED', 'SUPERSEDED'])
        self.assertEqual(rows[new]['payload']['supersedes'], [old])
        with self.assertRaisesRegex(ValueError, '只能由仍有效的提案替代'):
            governance.decide(self.store, new, 'SUPERSEDED', decided_by=None, note='x', replaced_by=old)
        with self.assertRaisesRegex(ValueError, '只有标记为SUPERSEDED'):
            governance.decide(self.store, new, 'READY', decided_by=None, note='x', replaced_by=old)
        text = weekly.markdown({**self.report(), 'proposals_closed': weekly.closed_proposals(self.store)})
        self.assertIn(f'SUPERSEDED（被新版替代，累计）：1 条；旧→新 {old}→{new}', text)

    def test_relabel_keeps_a_decision_made_before_history_existed(self):
        old, new = self.draft('旧版'), self.draft('新版')
        with self.store.db:  # a rejection recorded by 0.15.2: columns only, no history in the payload
            self.store.db.execute("UPDATE strategy_proposals SET status='REJECTED',decided_at=?,decided_by=?,decision_note=? WHERE id=?",
                                  ('2026-09-26T01:05:00+00:00', 'Dean', '被新版替代', old))
        with self.assertRaisesRegex(ValueError, '由 Dean 驳回'):
            governance.decide(self.store, old, 'SUPERSEDED', decided_by=None, note='改标', replaced_by=new)
        approved = self.draft('已批准')
        governance.decide(self.store, approved, 'READY', decided_by=None, note='整理')
        governance.decide(self.store, approved, 'APPROVED', decided_by='Dean', note='同意')
        with self.assertRaisesRegex(ValueError, '已批准'):
            governance.decide(self.store, approved, 'REJECTED', decided_by=None, note='代理改主意')
        governance.decide(self.store, old, 'SUPERSEDED', decided_by='Dean', note='改标', replaced_by=new)
        history = next(p for p in governance.proposals(self.store) if p['id'] == old)['payload']['history']
        self.assertEqual([(h['to'], h['by']) for h in history], [('REJECTED', 'Dean'), ('SUPERSEDED', 'Dean')])
        with self.store.db:
            forged = governance.draft_proposal(self.store, source='agent', kind='RULE', target='x', title='t',
                                               payload={'hypothesis': 'h', 'superseded_by': old, 'history': [{'to': 'ADOPTED'}]}, at='2026-09-26T01:00:00+00:00')
        payload = next(p for p in governance.proposals(self.store) if p['id'] == forged)['payload']
        self.assertNotIn('superseded_by', payload);self.assertNotIn('history', payload)

    def test_reports_follow_a_retitled_issue_through_every_title(self):
        with self.store.db:
            first = governance.record_issue(self.store, 'OTHER_DATA', None, 'd', [], '2026-09-26T01:00:00+00:00', title='甲', dedupe='甲')
        governance.retitle_issue(self.store, first, '乙', '更准确')
        governance.retitle_issue(self.store, first, '丙', '再改')
        with self.store.db:
            ids = {t: governance.record_issue(self.store, 'OTHER_DATA', None, 'd', [], '2026-09-26T02:00:00+00:00', title=t, dedupe=t) for t in ('甲', '乙', '丙')}
            auto = governance.record_issue(self.store, 'MISSING_DAILY_BARS', 'sh600001', 'd', [], '2026-09-26T02:00:00+00:00')
            manual = governance.record_issue(self.store, 'MISSING_DAILY_BARS', 'sh600001', 'd', [], '2026-09-26T02:00:00+00:00',
                                             title='日线缺失或未更新', dedupe='日线缺失或未更新')
        self.assertEqual(set(ids.values()), {first})
        self.assertNotEqual(auto, manual)  # a hand report under a default title does not merge into the automatic issue
        # Another issue retitled to a former title takes the reports under that title from then on.
        with self.store.db:
            other = governance.record_issue(self.store, 'OTHER_DATA', None, 'd', [], '2026-09-26T03:00:00+00:00', title='丁', dedupe='丁')
        governance.retitle_issue(self.store, other, '甲', '同名')
        with self.store.db:
            self.assertEqual(governance.record_issue(self.store, 'OTHER_DATA', None, 'd', [], '2026-09-26T04:00:00+00:00', title='甲', dedupe='甲'), other)

    def report(self):
        return {'window': {'from': '2026-09-19T00:00:00+00:00', 'to': '2026-09-26T00:00:00+00:00'}, 'horizon_days': 20,
                'registry': {'scored_this_week': 0, 'all_time': {'groups': {}}}, 'shadow': {'all_time': {}},
                'builds_this_week': [], 'model_usage': {'attempts': {}, 'portfolio_runs': {}, 'research_renewals_without_model': 0},
                'engineering_issues': [], 'proposals': {}, 'disk': {'free_gb': 1, 'db_gb': 1, 'backups_gb': 1, 'warning': False},
                'calendar_warning': None}

    def test_new_proposal_can_supersede_old_ones_in_one_command(self):
        from ashare.cli import _store_command
        old = self.draft('旧版')
        spec = Path(self.tmp.name) / 'p.json'
        spec.write_text(json.dumps({'kind': 'RULE', 'target': '自选股', 'title': '完整新版', 'hypothesis': 'h', 'change': 'c', 'evidence': 'e',
                                    'test_plan': 't', 'failure_criteria': 'f', 'rollback': 'r'}, ensure_ascii=False))
        args = argparse.Namespace(command='proposals', action='new', file=str(spec), id=None, status=None, to=None, approved_by=None,
                                  note=None, supersedes=['nope'], replaced_by=None)
        with self.assertRaisesRegex(ValueError, '未找到提案 nope'):
            _store_command(args, {}, self.store)
        self.assertEqual(len(governance.proposals(self.store)), 1)  # nothing written
        spec.write_text(json.dumps({'kind': 'RULE', 'target': '自选股', 'title': '完整新版', 'hypothesis': 'h', 'change': 'c', 'evidence': 'e',
                                    'test_plan': 't', 'failure_criteria': 'f', 'rollback': 'r', 'dedupe_key': 'same'}, ensure_ascii=False))
        with self.store.db:
            same = governance.draft_proposal(self.store, source='agent', kind='RULE', target='x', title='同键', payload={}, at='2026-09-26T01:00:00+00:00', dedupe_key='same')
        args.supersedes = [same]
        with self.assertRaisesRegex(ValueError, '替代它自己'):
            _store_command(args, {}, self.store)
        spec.write_text(json.dumps({'kind': 'RULE', 'target': '自选股', 'title': '完整新版', 'hypothesis': 'h', 'change': 'c', 'evidence': 'e',
                                    'test_plan': 't', 'failure_criteria': 'f', 'rollback': 'r'}, ensure_ascii=False))
        args.supersedes = [old]
        result = _store_command(args, {}, self.store)
        row = next(p for p in governance.proposals(self.store) if p['id'] == old)
        self.assertEqual((row['status'], row['payload']['superseded_by'], result['supersedes']), ('SUPERSEDED', result['id'], [old]))

    def test_retitled_issue_keeps_its_id_and_later_reports_collapse_into_it(self):
        from ashare.cli import _store_command
        args = argparse.Namespace(command='issues', action='new', id=None, status='OPEN', note=None, wontfix=False, key='OTHER_DATA',
                                  symbol=None, title='每股不足一分的现金分红无法核验', detail='0.055元', evidence=[])
        first = _store_command(args, {}, self.store)['id']
        retitle = argparse.Namespace(command='issues', action='retitle', id=first, status='OPEN', note='实际是含半分钱尾数', wontfix=False,
                                     key=None, symbol=None, title='每股现金分红含小数分时无法核验', detail=None, evidence=None)
        self.assertEqual(_store_command(retitle, {}, self.store)['to'], '每股现金分红含小数分时无法核验')
        args.title = '每股现金分红含小数分时无法核验'
        self.assertEqual(_store_command(args, {}, self.store), {'status': 'OPEN', 'id': first, 'occurrences': 2})
        args.title = '每股不足一分的现金分红无法核验'
        self.assertEqual(_store_command(args, {}, self.store)['id'], first)
        row = self.store.db.execute('SELECT title,payload_json FROM engineering_issues WHERE id=?', (first,)).fetchone()
        self.assertEqual(row['title'], '每股现金分红含小数分时无法核验')
        self.assertEqual(json.loads(row['payload_json'])['titles'][0]['note'], '实际是含半分钱尾数')
        args.title = '另一件事'
        other = _store_command(args, {}, self.store)['id']
        retitle.id, retitle.title = other, '每股现金分红含小数分时无法核验'
        with self.assertRaisesRegex(ValueError, first):
            _store_command(retitle, {}, self.store)
        retitle.note = ' '
        with self.assertRaisesRegex(ValueError, '理由'):
            _store_command(retitle, {}, self.store)


if __name__ == '__main__':
    unittest.main()
