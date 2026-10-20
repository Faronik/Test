"""Regressions reported after the first warehouse UI update."""
import json
import os
from pathlib import Path
import struct
import zlib
from playwright.sync_api import sync_playwright
from test_app import AppTest,app

out=Path(os.environ.get('TOOLSTOCK_TEST_OUTPUT','/tmp/toolstock-followup-ui'))
out.mkdir(parents=True,exist_ok=True)
checks=[]
def check(name, condition):
    checks.append({'name':name,'passed':bool(condition)})
    if not condition:raise AssertionError(name)

def png(width,height):
    def chunk(kind,payload):
        return struct.pack('!I',len(payload))+kind+payload+struct.pack('!I',zlib.crc32(kind+payload)&0xffffffff)
    row=b'\x00'+b'\xff\x55\x44'*10+b'\x44\xbb\xcc'*(width-20)+b'\x44\x55\xff'*10
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(row*height))+chunk(b'IEND',b'')

AppTest.setUpClass();fixture=AppTest();fixture.setUp()
try:
    pid=fixture.product('Пластина для проверки фото',category='Пластина',insert_kind='thread',thread_insert_designation='16ER 1.5 ISO')
    rid,request_product=fixture.purchase()
    with app.db() as con:
        for n in range(35):
            con.execute('INSERT INTO machines(name,location) VALUES(?,?)',(f'Станок {n:02d}','Цех'))
            wid=con.execute('INSERT INTO workers(name,personnel_no,machine_id) VALUES(?,?,1)',(f'Рабочий {n:02d}',f'P{n:03d}')).lastrowid
            con.execute('INSERT INTO worker_machines VALUES(?,1)',(wid,))
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
        context=browser.new_context(viewport={'width':1440,'height':1000})
        context.add_cookies([dict(name=c.name,value=c.value,domain='127.0.0.1',path='/') for c in fixture.cookies])
        page=context.new_page();errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        for width,height in ((1600,300),(300,1600)):
            page.goto(fixture.base+'/product/'+str(pid))
            page.locator('input[name=photo]').set_input_files({'name':f'photo-{width}.png','mimeType':'image/png','buffer':png(width,height)})
            with page.expect_navigation():page.locator('form[action="/product/save"] > button').click()
            img=page.locator('.product-photo img');img.wait_for()
            page.wait_for_function("document.querySelector('.product-photo img').naturalWidth>0")
            sizes=img.evaluate('(el)=>{const a=el.getBoundingClientRect(),b=el.parentElement.getBoundingClientRect();return {fits:a.width<=b.width&&a.height<=b.height,fit:getComputedStyle(el).objectFit,width:el.naturalWidth,height:el.naturalHeight}}')
            check(f'Фото {width}x{height}: корректные границы и пропорции',sizes['fits'] and sizes['fit']=='contain' and sizes['width']==width and sizes['height']==height)
            if width==1600:page.screenshot(path=str(out/'product-photo-desktop.png'),full_page=True)
        with page.expect_popup() as popup:page.locator('.product-photo').click()
        original=popup.value;original.wait_for_load_state()
        check('Открытие оригинала фото', '/photo/' in original.url);original.close()
        page.set_viewport_size({'width':390,'height':844})
        check('Карточка пластины на телефоне без переполнения',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        check('Фото в мобильной карточке помещается в блок',page.locator('.product-photo img').evaluate('(e)=>e.clientHeight<=e.parentElement.clientHeight&&e.clientWidth<=e.parentElement.clientWidth'))
        page.screenshot(path=str(out/'product-photo-mobile.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':1000})
        page.goto(fixture.base+'/catalog')
        check('Фото в каталоге доступно',page.locator(f'.thumb[src*="/photo/"]').count()>0)
        for viewport in (1440,768,390,320):
            page.set_viewport_size({'width':viewport,'height':1000})
            page.goto(fixture.base+'/transactions?op=receipt')
            field=page.locator('#receipt-search').bounding_box();button=page.locator('#add-receipt-product').bounding_box()
            check(f'Поступление {viewport}px: кнопка рядом с полем',abs(field['y']-button['y'])<=2 and abs(field['height']-button['height'])<=2 and button['x']>=field['x']+field['width'])
            check(f'Поступление {viewport}px: страница без переполнения',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
            if viewport==390:page.screenshot(path=str(out/'receipt-mobile.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':1000})
        page.goto(fixture.base+'/admin')
        check('Справочники: виден один раздел',page.locator('.admin-section:visible').count()==1)
        listing=page.locator('#admin-machines > .directory-list').first
        check('Большой список станков ограничен и прокручивается',listing.evaluate('(e)=>e.clientHeight<=400&&e.scrollHeight>e.clientHeight'))
        page.locator('#admin-machines [data-directory-search]').first.fill('СТАНОК 30')
        check('Поиск станка в длинном списке',listing.locator('.directory-row:visible').count()==1 and 'Станок 30' in listing.inner_text())
        page.locator('[data-admin-tab=workers]').click()
        check('Переключение на рабочих',page.locator('#admin-workers').is_visible() and not page.locator('#admin-machines').is_visible())
        checklist=page.locator('#admin-workers > form .machine-check-grid')
        check('Выбор станков ограничен и прокручивается',checklist.evaluate('(e)=>e.clientHeight<=220&&e.scrollHeight>e.clientHeight'))
        page.locator('#admin-workers > form [data-machine-search]').fill('СТАНОК 31')
        check('Поиск станка при создании рабочего',checklist.locator('.machine-check:visible').count()==1)
        page.locator('#admin-workers > form [name=name]').fill('Новый рабочий')
        page.locator('#admin-workers > form [name=personnel_no]').fill('NEW-1')
        checklist.locator('.machine-check:visible input').check()
        with page.expect_navigation():page.locator('#admin-workers > form > button').click()
        check('Создание рабочего сохраняет выбранный раздел',page.locator('#admin-workers').is_visible() and 'section=workers' in page.url)
        workerlist=page.locator('#admin-workers > .directory-list').first
        check('Большой список рабочих ограничен',workerlist.evaluate('(e)=>e.clientHeight<=400&&e.scrollHeight>e.clientHeight'))
        page.locator('#admin-workers [data-directory-search]').first.fill('NEW-1')
        check('Поиск рабочего по табельному номеру',workerlist.locator('.directory-row:visible').count()==1)
        page.screenshot(path=str(out/'directories-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        check('Справочники на телефоне без переполнения',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        page.screenshot(path=str(out/'directories-mobile.png'),full_page=True)
        page.set_viewport_size({'width':1440,'height':1000})
        page.goto(fixture.base+'/purchasing')
        row=page.locator(f'[data-request-id="{rid}"]')
        page.once('dialog',lambda d:d.accept())
        with page.expect_navigation():row.get_by_role('button',name='Закрыть заказ').click()
        row=page.locator(f'[data-request-id="{rid}"]')
        check('Заказ закрывается из интерфейса','view=closed' in page.url and row.locator('.tag').inner_text()=='Закрыта')
        check('Закрытие заказа не создаёт складское поступление',fixture.balance(request_product)==0)
        page.screenshot(path=str(out/'purchases-closed.png'),full_page=True)
        page.once('dialog',lambda d:d.accept())
        with page.expect_navigation():row.get_by_role('button',name='Удалить из списка').click()
        check('Заявка убрана из основного списка',page.locator(f'[data-request-id="{rid}"]').count()==0)
        page.get_by_role('link',name='Архив',exact=True).click()
        row=page.locator(f'[data-request-id="{rid}"]')
        check('Заявка и выгрузка доступны в архиве',row.count()==1 and row.get_by_role('link',name='Выгрузить CSV').count()==1)
        with page.expect_navigation():row.get_by_role('button',name='Вернуть в список').click()
        check('Восстановление возвращает завершённый заказ', 'view=closed' in page.url and page.locator(f'[data-request-id="{rid}"] .tag').inner_text()=='Закрыта')
        check('Все новые сценарии без ошибок JavaScript',not errors)
        browser.close()
finally:
    (out/'followup-results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2))
    fixture.tearDown();AppTest.tearDownClass()
print('Follow-up browser checks passed:',len(checks))
