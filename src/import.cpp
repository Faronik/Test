#include "core.hpp"
#include <sqlite3.h>
#include <iostream>
#include <regex>
void import_sqlite(DB&db,const fs::path&source,const fs::path&photo_source,const fs::path&dest){
 struct Staging{fs::path path;~Staging(){std::error_code e;fs::remove_all(path,e);}} staging{fs::temp_directory_path()/("toolstock-import-"+random_hex(12))};
 fs::create_directories(staging.path);auto snapshot=staging.path/"source.sqlite3";
 need(fs::is_regular_file(source),"Исходный SQLite-файл не найден");fs::copy_file(source,snapshot);
 auto wal=fs::path(source.string()+"-wal");if(fs::exists(wal)&&fs::file_size(wal)>0)fs::copy_file(wal,snapshot.string()+"-wal");
 // Only the temporary copy may create SQLite's WAL shared-memory files.
 sqlite3*raw=nullptr;need(sqlite3_open_v2(snapshot.c_str(),&raw,SQLITE_OPEN_READONLY,nullptr)==SQLITE_OK,"Не удалось открыть копию SQLite-базы");
 std::unique_ptr<sqlite3,decltype(&sqlite3_close)>sqlite(raw,sqlite3_close);
 sqlite3_exec(raw,"BEGIN",nullptr,nullptr,nullptr);sqlite3_stmt*check=nullptr;need(sqlite3_prepare_v2(raw,"PRAGMA integrity_check",-1,&check,nullptr)==SQLITE_OK,"Не удалось проверить копию SQLite");bool integrity=sqlite3_step(check)==SQLITE_ROW&&std::string((const char*)sqlite3_column_text(check,0))=="ok";sqlite3_finalize(check);need(integrity,"Исходная база повреждена");
 std::vector<fs::path>created;bool committing=false;db.begin();
 try{
 need(db.one("SELECT (SELECT count(*) FROM users)+(SELECT count(*) FROM products)+(SELECT count(*) FROM movements)+(SELECT count(*) FROM purchase_requests) n")["n"]==0,"Импорт разрешён только в пустую PostgreSQL-базу");
 const std::vector<std::string>tables={"machines","workers","role_definitions","users","worker_machines","products","movements","movement_changes","purchase_requests","purchase_request_lines"};
 std::set<std::string>integer_fields={"qty","min_qty","stock_at_request","min_at_request"};
 for(auto&table:tables){
 sqlite3_stmt*stmt=nullptr;auto sql="SELECT * FROM "+table;
 if(sqlite3_prepare_v2(raw,sql.c_str(),-1,&stmt,nullptr)!=SQLITE_OK){if(table=="purchase_requests"||table=="purchase_request_lines"||table=="movement_changes")continue;throw Error(400,"В исходной базе нет таблицы "+table);}
 std::unique_ptr<sqlite3_stmt,decltype(&sqlite3_finalize)>guard(stmt,sqlite3_finalize);
 auto target_cols=db.q("SELECT column_name FROM information_schema.columns WHERE table_name=$1 AND table_schema='public'",{table});std::set<std::string>columns;for(auto&c:target_cols)columns.insert(str(c,"column_name"));
 std::vector<int>indices;std::vector<std::string>names;for(int i=0;i<sqlite3_column_count(stmt);i++){std::string name=sqlite3_column_name(stmt,i);need(columns.contains(name),"Неизвестное поле "+table+"."+name);indices.push_back(i);names.push_back(name);}
 size_t count=0;int code;
 while((code=sqlite3_step(stmt))==SQLITE_ROW){
 std::vector<Param>values;for(size_t k=0;k<indices.size();k++){int i=indices[k];if(sqlite3_column_type(stmt,i)==SQLITE_NULL){values.push_back({});continue;}std::string value((const char*)sqlite3_column_text(stmt,i));if(integer_fields.contains(names[k])){try{size_t used;double n=std::stod(value,&used);need(used==value.size()&&std::isfinite(n)&&n>=0&&std::floor(n)==n&&n<=9007199254740991.0,"Не поддерживается дробное количество в "+table);value=std::to_string((long long)n);}catch(const std::invalid_argument&){throw Error(400,"Неверное количество в "+table);}}values.push_back(value);}
 std::string insert="INSERT INTO "+table+"(";for(size_t k=0;k<names.size();k++)insert+=(k?",":"")+names[k];insert+=") VALUES(";for(size_t k=0;k<names.size();k++)insert.append(k?",":"").append("$").append(std::to_string(k+1));insert+=")";
 if(table=="role_definitions"){insert+=" ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,permissions_json=EXCLUDED.permissions_json,is_system=EXCLUDED.is_system";}
 db.q(insert,values);count++;
 }
 need(code==SQLITE_DONE,"Ошибка чтения SQLite: "+table);auto total=db.one("SELECT count(*) n FROM "+table)["n"].get<size_t>();if(table!="role_definitions")need(count==total,"Число записей после переноса не совпало: "+table);std::cout<<table<<": "<<count<<"\n";
 if(columns.contains("id"))db.q("SELECT setval(pg_get_serial_sequence('"+table+"','id'),COALESCE(max(id),1),max(id) IS NOT NULL) FROM "+table);
 }
 need(db.q("SELECT 1 FROM inventory WHERE qty<0 UNION ALL SELECT 1 FROM holdings WHERE qty<0").empty(),"Исходные данные содержат отрицательные остатки. Сначала выполните сверку; импорт отменён");
 fs::create_directories(dest);
 for(auto&r:db.q("SELECT DISTINCT photo FROM products WHERE photo IS NOT NULL AND photo<>''")){
 auto file=str(r,"photo");need(std::regex_match(file,std::regex("[a-f0-9]{32}\\.(png|jpg|jpeg|webp)")),"Неподдерживаемое имя фото");auto from=photo_source/file,to=dest/file;need(fs::is_regular_file(from),"Исходная фотография отсутствует: "+file);if(fs::exists(to))need(sha256(readfile(from))==sha256(readfile(to)),"В целевой папке другое фото с тем же именем");else{fs::copy_file(from,to);created.push_back(to);}
 }
 committing=true;db.commit();std::cout<<"Import committed. Source unchanged; sessions were not copied.\n";
 }catch(...){try{db.rollback();}catch(...){}if(!committing)for(auto&p:created){std::error_code e;fs::remove(p,e);}throw;}
}
