// Optional migration validation requires Node.js 24 (node:sqlite) and Docker.
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {mkdtempSync,writeFileSync,readFileSync,copyFileSync,chmodSync,mkdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
import {randomUUID,randomBytes,pbkdf2Sync,createHash} from 'node:crypto';
const project=process.env.TOOLSTOCK_TEST_PROJECT;
assert(project,'Set TOOLSTOCK_TEST_PROJECT to a disposable Compose project.');
const suffix=randomBytes(5).toString('hex'),database='migration_'+suffix,badDatabase=database+'_bad',helper='toolstock-import-'+suffix,http='toolstock-import-http-'+suffix;
const folder=mkdtempSync(join(tmpdir(),'toolstock-migration-'));chmodSync(folder,0o755);mkdirSync(join(folder,'photos'),{mode:0o755});chmodSync(join(folder,'photos'),0o755);
const photo=randomBytes(16).toString('hex')+'.png',password='legacy-test-password',salt=randomBytes(16),hash='pbkdf2_sha256$310000$'+salt.toString('hex')+'$'+pbkdf2Sync(password,salt,310000,32,'sha256').toString('hex');
const timestamp=new Date().toISOString(),file=join(folder,'toolstock.sqlite3');
const sqlite=new DatabaseSync(file);sqlite.exec(readFileSync(new URL('./legacy-schema.sql',import.meta.url),'utf8'));
const run=(sql,...args)=>sqlite.prepare(sql).run(...args);
run("INSERT INTO machines(id,name) VALUES(1,'Станок из SQLite')");run("INSERT INTO workers(id,name,machine_id) VALUES(1,'Рабочий из SQLite',1)");run('INSERT INTO worker_machines VALUES(1,1)');
for(const [code,name,perms]of [['ADMIN','Администратор',[]],['WORKER','Рабочий',['dashboard','catalog.view','reports.view']],['CUSTOM_MIGRATION','Перенесённая роль',['catalog.view']]])run('INSERT INTO role_definitions VALUES(?,?,?,?)',code,name,JSON.stringify(perms),code.startsWith('CUSTOM')?0:1);
run("INSERT INTO users(id,username,password_hash,role,created_at) VALUES(1,'legacyadmin',?,'ADMIN',?)",hash,timestamp);
run("INSERT INTO products(id,code,item_class,category,name,params_json,min_qty,price_per_unit,photo,created_at) VALUES(9,'INS-000009','consumable','Прочее','Фреза SQLite',?,5,120,?,?)",JSON.stringify({diameter:10,radius:0.8}),photo,timestamp);
for(const [kind,qty,from,to,w,m]of [['receipt',7,null,'new',null,null],['issue',2,'new','new',1,1],['return',1,'new','used',1,1]])run('INSERT INTO movements(batch_id,occurred_at,product_id,kind,qty,state_from,state_to,worker_id,machine_id,actor_user_id) VALUES(?,?,9,?,?,?,?,?,?,1)',randomUUID(),timestamp,kind,qty,from,to,w,m);
run("INSERT INTO movement_changes(movement_id,changed_at,actor_user_id,action,reason,before_json,after_json) VALUES(1,?,1,'edit','Перенос аудита','{\"qty\":8}','{\"qty\":7}')",timestamp);
run("INSERT INTO purchase_requests(id,request_no,created_at,created_by,status,closed_at,closed_by) VALUES(10,'ZAK-000010',?,1,'Закрыта',?,1)",timestamp,timestamp);
run("INSERT INTO purchase_request_lines(request_id,product_id,code,name,category,qty,unit,unit_price,stock_at_request,min_at_request) VALUES(10,9,'INS-000009','Фреза SQLite','Прочее',5,'шт',120,0,5)");sqlite.close();chmodSync(file,0o644);
copyFileSync(new URL('../web/logo.png',import.meta.url),join(folder,'photos',photo));chmodSync(join(folder,'photos',photo),0o644);
const sourceHash=createHash('sha256').update(readFileSync(file)).digest('hex');
const compose=['compose','-p',project];
function cmd(args,expected=0){const r=spawnSync('docker',args,{encoding:'utf8'});assert.equal(r.status,expected,`docker ${args.slice(0,3).join(' ')} failed: ${r.stderr}`);return r.stdout;}
const sql=(db,q)=>cmd([...compose,'exec','-T','db','psql','-U','toolstock','-d',db,'-tAc',q]).trim();
let passed=0;
try{
 cmd([...compose,'exec','-T','db','createdb','-U','toolstock',database]);cmd([...compose,'exec','-T','db','createdb','-U','toolstock',badDatabase]);
 cmd([...compose,'run','-d','--no-deps','--name',helper,'-e','PGDATABASE='+database,'app','sleep','600']);cmd(['cp',folder+'/.',helper+':/tmp/legacy']);
 cmd(['exec',helper,'toolstock','--import-sqlite','/tmp/legacy/toolstock.sqlite3','/tmp/legacy/photos']);
 assert.equal(sql(database,'SELECT count(*) FROM movements'),'3');assert.equal(sql(database,'SELECT count(*) FROM movement_changes'),'1');assert.equal(sql(database,"SELECT status FROM purchase_requests WHERE id=10"),'Закрыта');passed++;
 cmd(['exec',helper,'toolstock','--import-sqlite','/tmp/legacy/toolstock.sqlite3','/tmp/legacy/photos'],1);assert.equal(sql(database,'SELECT count(*) FROM products'),'1');passed++;
 cmd(['exec','-e','PGDATABASE='+badDatabase,helper,'toolstock','--import-sqlite','/tmp/legacy/toolstock.sqlite3','/missing-photos'],1);
 assert.equal(sql(badDatabase,'SELECT (SELECT count(*) FROM products)+(SELECT count(*) FROM users)+(SELECT count(*) FROM movements)'), '0');passed++;
 const port=process.env.TOOLSTOCK_MIGRATION_PORT||'18081';cmd([...compose,'run','-d','--no-deps','--name',http,'-p','127.0.0.1:'+port+':8000','-e','PGDATABASE='+database,'app']);const base='http://127.0.0.1:'+port;
 for(let i=0;i<50;i++){try{if((await fetch(base+'/health')).ok)break;}catch(_){}await new Promise(r=>setTimeout(r,200));}
 const login=await(await fetch(base+'/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'legacyadmin',password})})).json();assert(login.token,'Legacy password did not authenticate');passed++;
 const headers={Authorization:'Bearer '+login.token};const product=await(await fetch(base+'/api/v1/products/9',{headers})).json();assert.equal(product.stocks.new,5);assert.equal(product.stocks.used,1);assert.equal(product.params_json.radius,0.8);assert.equal(product.photo,photo);passed++;
 const holds=await(await fetch(base+'/api/v1/holdings',{headers})).json();assert.equal(holds[0].qty,1);assert.equal((await fetch(base+'/photo/'+photo,{headers})).status,200);passed++;
 const roles=await(await fetch(base+'/api/v1/roles',{headers})).json();assert(roles.some(r=>r.code==='CUSTOM_MIGRATION'));passed++;
 const next=await(await fetch(base+'/api/v1/products',{method:'POST',headers:{...headers,'Content-Type':'application/json','Idempotency-Key':randomUUID()},body:JSON.stringify({name:'После переноса',category:'Прочее',item_class:'consumable'})})).json();assert(next.id>9);passed++;
 assert.equal(createHash('sha256').update(readFileSync(file)).digest('hex'),sourceHash);passed++;
 console.log('Migration checks passed: '+passed);
}finally{
 spawnSync('docker',['rm','-f',http],{stdio:'ignore'});
 spawnSync('docker',['exec',helper,'rm','-f','/var/lib/toolstock/photos/'+photo],{stdio:'ignore'});
 spawnSync('docker',['rm','-f',helper],{stdio:'ignore'});
 for(const db of [database,badDatabase])spawnSync('docker',[...compose,'exec','-T','db','dropdb','-U','toolstock','--if-exists','--force',db],{stdio:'ignore'});
 rmSync(folder,{recursive:true,force:true});
}
