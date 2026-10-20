"""Optional developer test. Requires Playwright, Chromium and API test fixtures."""
import json,os,uuid
from pathlib import Path
from playwright.sync_api import sync_playwright
base=os.environ.get('TOOLSTOCK_TEST_URL','http://127.0.0.1:8000')
login=json.loads(Path(os.environ.get('TOOLSTOCK_TEST_LOGIN','/tmp/toolstock-cpp-test-login.json')).read_text())
out=Path(os.environ.get('TOOLSTOCK_TEST_OUTPUT','/tmp/toolstock-cpp-ui'));out.mkdir(exist_ok=True,parents=True)
checks=[]
def check(name,yes):
 checks.append({'name':name,'passed':bool(yes)})
 assert yes,name
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
 context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page();errors=[]
 page.on('pageerror',lambda e:(errors.append(str(e)),print('PAGE ERROR',str(e),flush=True)))
 try:
  page.goto(base)
  page.locator('form[data-form=login] [name=username]').fill(login['username']);page.locator('[name=password]').fill(login['password']);page.locator('form[data-form=login] button').click()
  page.locator('.sidebar').wait_for();check('Вход через интерфейс',page.locator('.sidebar').is_visible())
  page.goto(base+'/#catalog');page.get_by_role('link',name='Добавить позицию',exact=True).click();page.locator('form[data-form=product]').wait_for()
  form=page.locator('form[data-form=product]');form.locator('[name=category]').select_option(label='Прочее');form.locator('[name=name]').fill('Браузерная фреза');form.locator('[name=model]').fill('BROWSER-MODEL');form.locator('[name=diameter]').fill('10.5');form.locator('[name=radius]').fill('0.8');form.locator('[name=length]').fill('90');form.locator('[name=min_qty]').fill('5');form.locator('[name=location]').fill('А-22');form.locator('[name=photo]').set_input_files(str(Path(__file__).resolve().parents[1]/'web/logo.png'));form.get_by_role('button',name='Сохранить карточку').click()
  page.locator('.notice').filter(has_text='Карточка сохранена').wait_for();pid=page.url.split('#product/')[1]
  check('Создание карточки с фото',page.locator('.product-photo img').count()==1)
  check('Дробный радиус и длина сохраняются',page.locator('form[data-form=product] [name=radius]').input_value()=='0.8' and page.locator('form[data-form=product] [name=length]').input_value()=='90')
  page.get_by_role('link',name='Поступление',exact=True).click();page.locator('form[data-operation=receipt] [name=qty]').fill('10');page.get_by_role('button',name='Провести операцию').click();page.locator('dialog').wait_for();page.locator('[data-confirm=yes]').click();page.locator('.notice').filter(has_text='Операция проведена').wait_for()
  check('Поступление через C++ API из интерфейса',True)
  page.goto(base+'/#operations?op=issue&product_id='+pid);form=page.locator('form[data-operation=issue]');form.locator('[name=worker_id]').select_option(label='Иванов Иван');form.locator('[name=machine_id]').select_option(label='Станок 1');form.locator('[name=qty]').fill('3');form.get_by_role('button',name='Провести операцию').click();page.locator('dialog').wait_for();check('Предварительный остаток и получатель','7' in page.locator('.confirmation-text').inner_text() and 'Иванов' in page.locator('.confirmation-text').inner_text());page.locator('[data-confirm=yes]').click();page.locator('.notice').filter(has_text='Операция проведена').wait_for()
  page.goto(base+'/#operations?op=settle');form=page.locator('form[data-operation=settle]');form.locator('[name=worker_id]').select_option(label='Иванов Иван');holding=form.locator('[name=holding]');idx=holding.locator('option').all_text_contents();chosen=next(i for i,text in enumerate(idx) if 'Браузерная фреза' in text);holding.select_option(index=chosen);check('Количество частичной сдачи не подставляется',form.locator('[name=qty]').input_value()=='');form.locator('[name=qty]').fill('1');form.get_by_role('button',name='Провести операцию').click();page.locator('[data-confirm=yes]').click();page.locator('.notice').filter(has_text='Операция проведена').wait_for();check('Частичный возврат',True)
  # Regrind queue and receipt via API-backed forms.
  page.goto(base+'/#operations?op=settle');form=page.locator('form[data-operation=settle]');form.locator('[name=worker_id]').select_option(label='Иванов Иван');holding=form.locator('[name=holding]');labels=holding.locator('option').all_text_contents();holding.select_option(index=next(i for i,x in enumerate(labels) if 'Браузерная фреза' in x));form.locator('[name=action]').select_option('regrind');form.locator('[name=qty]').fill('2');form.get_by_role('button',name='Провести операцию').click();page.locator('[data-confirm=yes]').click();page.locator('.notice').wait_for()
  page.goto(base+'/#operations?op=regrind-send');form=page.locator('form[data-operation=regrind-send]');form.locator('[name=product_id]').select_option(pid);form.locator('[name=qty]').fill('2');form.get_by_role('button',name='Провести операцию').click();page.locator('[data-confirm=yes]').click();page.locator('.notice').wait_for()
  page.goto(base+'/#operations?op=regrind-receive');form=page.locator('form[data-operation=regrind-receive]');form.locator('[name=product_id]').select_option(pid);form.locator('[name=diameter]').fill('9.8');form.locator('[name=qty]').fill('2');form.get_by_role('button',name='Провести операцию').click();page.locator('[data-confirm=yes]').click();page.locator('.notice').wait_for();check('Полный цикл переточки через интерфейс',True)
  page.goto(base+'/#product/'+pid);page.locator('.product-photo img').wait_for();page.wait_for_function("document.querySelector('.product-photo img').naturalWidth>0");check('Фото помещается в карточке',page.locator('.product-photo img').evaluate('(e)=>e.clientWidth<=e.parentElement.clientWidth&&e.clientHeight<=e.parentElement.clientHeight&&getComputedStyle(e).objectFit==="contain"'))
  page.screenshot(path=str(out/'product.png'),full_page=True)
  page.goto(base+'/#catalog');page.locator('[data-filter=q]').fill('БРАУЗЕРНАЯ');check('Поиск кириллицы в каталоге',page.locator('.product-card').count()>=1)
  page.set_viewport_size({'width':390,'height':844});check('Каталог на телефоне',page.evaluate('document.documentElement.scrollWidth<=innerWidth'));page.screenshot(path=str(out/'mobile-catalog.png'),full_page=True)
  page.goto(base+'/#operations?op=receipt');page.locator('#receipt-picker').wait_for();a=page.locator('[data-product-search]').bounding_box();b=page.locator('[data-action=inline-product]').bounding_box();check('Кнопка создания рядом с полем',abs(a['y']-b['y'])<2);page.locator('[data-action=inline-product]').click();form=page.locator('form[data-form=operation]');form.locator('[name=category]').select_option(label='Прочее');form.locator('[name=name]').fill('Новая из поступления');form.locator('[name=qty]').fill('2');form.get_by_role('button',name='Провести операцию').click();page.locator('[data-confirm=yes]').click();page.locator('.notice').wait_for();check('Карточка и поступление одной операцией',True)
  page.set_viewport_size({'width':1440,'height':1000});page.goto(base+'/#purchasing');page.locator('form[data-form=purchase]').get_by_role('button',name='Сформировать заявку').click();page.locator('.notice').wait_for();check('Создание заявки',True);page.locator('[data-action=purchase-close]').first.click();page.locator('[data-confirm=yes]').click();page.wait_for_url('**/#purchasing?view=closed');page.locator('[data-action=purchase-archive]').first.click();page.locator('[data-confirm=yes]').click();page.wait_for_url('**/#purchasing?view=open');page.goto(base+'/#purchasing?view=archived');page.locator('[data-action=purchase-restore]').first.wait_for();check('Завершение и удаление заявки в архив',True)
  page.goto(base+'/#admin?tab=machines');page.locator('form[data-form=directory] [name=name]').first.fill('Станок браузера '+uuid.uuid4().hex[:6]);page.locator('form[data-form=directory] button[type=submit]').first.click();page.locator('.notice').wait_for();check('Справочник: создание станка',True)
  page.locator('[data-directory-search]').fill('window.xss');check('Сохранённый скрипт не выполняется',page.evaluate('window.xss!==1'))
  page.goto(base+'/#reports');page.locator('form[data-form=history-filter]').wait_for();check('Отчёты и журнал',page.locator('.history-table').count()==1)
  page.goto(base+'/api/docs');page.locator('#endpoint option').nth(2).wait_for(state='attached');check('Интерактивное API без CDN',page.locator('#endpoint option').count()>30)
  check('Нет ошибок JavaScript',not errors)
 finally:
  (out/'results.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2));browser.close()
print('Browser checks passed:',len(checks))
