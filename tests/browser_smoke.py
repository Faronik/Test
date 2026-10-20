"""Optional UI regression checks; requires Playwright and Chromium."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote
from playwright.sync_api import sync_playwright
from test_app import AppTest

out=Path(os.environ.get('TOOLSTOCK_TEST_OUTPUT','/tmp/toolstock-ui-results'))
out.mkdir(parents=True,exist_ok=True)
AppTest.setUpClass();fixture=AppTest();fixture.setUp()
checks=[]
def check(name, condition):
    checks.append({'name':name,'passed':bool(condition)})
    if not condition:raise AssertionError(name)

try:
    pid=fixture.product('Фреза концевая',manufacturer='Завод',location='А-12',diameter='10',radius='0.8')
    fixture.receipt(pid,10)
    second=fixture.product('Сверло',model='DRILL',location='Б-03')
    fixture.receipt(second,2)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1440,'height':1000})
        context.add_cookies([dict(name=c.name,value=c.value,domain='127.0.0.1',path='/') for c in fixture.cookies])
        page=context.new_page();errors=[]
        page.on('pageerror',lambda e:(errors.append({'url':page.url,'error':str(e)}),print('PAGE ERROR',page.url,e.stack,flush=True)))
        for route in ['dashboard','catalog','transactions','reports','admin','product/'+str(pid),'product/new']:
            page.goto(fixture.base+'/'+route)
        check('Основные экраны без ошибок JavaScript',not errors)
        page.goto(fixture.base+'/transactions')
        check('Виден один экран операции',page.locator('.operation-panel:visible').count()==1)
        page.locator('[data-op=issue]').click()
        check('Переключение на выдачу',page.locator('#op-issue').is_visible() and not page.locator('#op-receipt').is_visible())
        page.reload();check('Выбранная операция сохраняется после перезагрузки',page.locator('#op-issue').is_visible())
        page.goto(fixture.base+'/product/'+str(pid))
        page.locator('.quick-actions a').filter(has_text='Выдать').click()
        page.wait_for_function("document.getElementById('issue-product-id').value !== ''")
        check('Быстрый переход предвыбирает карточку',page.locator('#issue-product-id').input_value()==str(pid))
        page.locator('#issue-form [name=worker_id]').select_option('1')
        page.locator('#issue-form [name=machine_id]').select_option('1')
        page.locator('#issue-form [name=qty]').fill('3')
        page.locator('#issue-form .primary').click()
        check('Подтверждение содержит остаток и получателя',page.locator('dialog').is_visible() and '7 шт' in page.locator('dialog').inner_text() and 'Иванов' in page.locator('dialog').inner_text())
        page.locator('dialog [data-cancel]').click()
        check('Возврат из подтверждения сохраняет форму',page.locator('#issue-form [name=qty]').input_value()=='3' and fixture.balance(pid)==10)
        page.locator('#issue-form .primary').click()
        with page.expect_navigation():page.locator('dialog [data-approve]').click()
        check('Выдача через браузер',fixture.balance(pid)==7)
        # Settlement requires explicit count, supports partial returns.
        page.locator('[data-op=settle]').click()
        page.locator('#settle-form [name=worker_id]').select_option('1')
        page.locator('#holding-select option').nth(1).wait_for(state='attached')
        page.locator('#holding-select').select_option('0')
        check('При сдаче количество не подставляется автоматически',page.locator('#settle-form [name=qty]').input_value()=='')
        page.locator('#settle-form [name=qty]').fill('1');page.locator('#settle-form .primary').click()
        with page.expect_navigation():page.locator('dialog [data-approve]').click()
        check('Частичный возврат через браузер',fixture.balance(pid,'used')==1 and fixture.balance(pid,worker_id=1,machine_id=1)==2)
        # Stale snapshot fails without losing user input.
        page.goto(fixture.base+'/transactions?op=issue&product_id='+str(second))
        page.wait_for_function("document.getElementById('issue-product-id').value !== ''")
        fixture.post('/transaction/writeoff',product_id=second,qty=2,state='new',note='Брак')
        page.locator('#issue-form [name=worker_id]').select_option('1');page.locator('#issue-form [name=machine_id]').select_option('1');page.locator('#issue-form [name=qty]').fill('1');page.locator('#issue-form [name=note]').fill('Не потерять комментарий')
        page.locator('#issue-form .primary').click();page.locator('dialog [data-approve]').click()
        page.locator('#issue-form .form-error').wait_for()
        check('Серверная ошибка показана рядом с формой', 'Недостаточный остаток' in page.locator('#issue-form .form-error').inner_text())
        check('При ошибке данные формы сохранены',page.locator('#issue-form [name=qty]').input_value()=='1' and page.locator('#issue-form [name=note]').input_value()=='Не потерять комментарий')
        # Keyboard search and stale selection reset.
        page.goto(fixture.base+'/transactions?op=receipt')
        page.locator('#receipt-search').fill('фреза');page.locator('#receipt-results button').first.wait_for()
        page.locator('#receipt-search').press('ArrowDown');page.keyboard.press('Enter')
        check('Выбор результата клавиатурой',page.locator('#receipt-product-id').input_value()==str(pid))
        page.locator('#receipt-search').fill('Нет такой позиции')
        check('Старая карточка сбрасывается при изменении поиска',page.locator('#receipt-product-id').input_value()=='')
        # Fractional radius can be saved through HTML validation, not only HTTP.
        page.goto(fixture.base+'/product/'+str(pid))
        page.locator('[name=radius]').fill('0.8')
        check('Поле радиуса принимает дробь',page.locator('[name=radius]').evaluate('(el)=>el.checkValidity()'))
        # Inline creation + receipt; no invisible required fields block submission.
        page.goto(fixture.base+'/transactions?op=receipt')
        page.locator('#add-receipt-product').click()
        page.locator('[name=new_category]').select_option(label='Прочее')
        page.locator('[name=new_name]').fill('Новая позиция из поступления')
        page.locator('[name=new_model]').fill('INLINE-NEW')
        page.locator('#receipt-form [name=qty]').fill('2')
        page.locator('#receipt-form .primary').click()
        with page.expect_navigation():page.locator('dialog [data-approve]').click()
        inline=fixture.scalar("SELECT id FROM products WHERE model='INLINE-NEW'")
        check('Новая карточка и поступление из одной формы',inline and fixture.balance(inline)==2)
        # Reports mapping.
        page.goto(fixture.base+'/reports');page.locator('#report-worker').select_option('1')
        check('Фильтры отчётов синхронизируются',page.locator('#report-machine').get_attribute('required') is not None)
        # Filter return and sort round-trip.
        page.goto(fixture.base+'/catalog?q='+quote('фреза')+'&manufacturer='+quote('Завод'))
        page.locator('.catalog-table .text-link').filter(has_text='Открыть').first.click()
        check('Возврат в каталог сохраняет фильтр', 'manufacturer=' in page.locator('.catalog-back').get_attribute('href'))
        # Screenshot actual populated views.
        page.goto(fixture.base+'/transactions?op=issue&product_id='+str(pid))
        page.wait_for_function("document.getElementById('issue-product-id').value !== ''")
        page.screenshot(path=str(out/'operations-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844});page.goto(fixture.base+'/catalog')
        page.screenshot(path=str(out/'catalog-mobile.png'),full_page=True)
        check('Мобильный каталог без горизонтальной прокрутки',page.locator('.catalog-table').evaluate('(e)=>e.scrollWidth<=e.clientWidth') and page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        row=page.locator('.catalog-table tbody tr').first
        check('На мобильном видны остаток, ячейка и действие',row.locator('td').nth(8).is_visible() and row.locator('td').nth(9).is_visible() and row.locator('td').nth(10).is_visible())
        page.locator('.mobile-menu').click();check('Мобильное меню открывается',page.locator('#main-navigation').is_visible())
        page.set_viewport_size({'width':1440,'height':1000})
        payload='</script><script>window.auditStoredXSS=1</script>'
        fixture.post('/admin/machine',name=payload)
        fixture.post('/admin/worker',name='Проверка безопасности',machine_ids=['3'])
        page.goto(fixture.base+'/transactions')
        check('Сохранённый скрипт из справочника не выполняется',not page.evaluate('window.auditStoredXSS===1'))
        check('Нет ошибок JavaScript после всех сценариев',not errors)
        browser.close()
finally:
    (out/'browser-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    fixture.tearDown();AppTest.tearDownClass()
print(f'Browser checks passed: {len(checks)}')
