#pragma once
#include <libpq-fe.h>
#include <nlohmann/json.hpp>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <openssl/crypto.h>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <optional>
#include <stdexcept>
#include <set>
#include <sstream>
#include <iomanip>
#include <algorithm>
#include <cmath>
using json=nlohmann::json;
namespace fs=std::filesystem;
using Param=std::optional<std::string>;
struct Error:std::runtime_error{int status;Error(int s,const std::string&m):runtime_error(m),status(s){}};
inline void need(bool ok,const std::string&m,int status=400){if(!ok)throw Error(status,m);}
inline std::string env(const char*k,const std::string&fallback=""){auto v=std::getenv(k);return v?v:fallback;}
inline std::string readfile(const fs::path&p){std::ifstream f(p,std::ios::binary);if(!f)throw std::runtime_error("Cannot read resource: "+p.string());return {std::istreambuf_iterator<char>(f),{}};}
inline std::string str(const json&j,const std::string&k,const std::string&fallback=""){if(!j.contains(k)||j[k].is_null())return fallback;return j[k].is_string()?j[k].get<std::string>():j[k].dump();}
inline std::string hex(const unsigned char*data,size_t n){std::ostringstream s;for(size_t i=0;i<n;i++)s<<std::hex<<std::setw(2)<<std::setfill('0')<<(int)data[i];return s.str();}
inline std::string random_hex(size_t n=32){std::vector<unsigned char>b(n);if(RAND_bytes(b.data(),n)!=1)throw std::runtime_error("Random generator failed");return hex(b.data(),b.size());}
inline std::string sha256(const std::string&s){unsigned char out[32];unsigned int n=0;EVP_Digest(s.data(),s.size(),out,&n,EVP_sha256(),nullptr);return hex(out,n);}
inline std::string password_hash(const std::string&password,const std::string&salt="",int iterations=310000){std::string salt_hex=salt.empty()?random_hex(16):salt;std::vector<unsigned char>b;for(size_t i=0;i<salt_hex.size();i+=2)b.push_back(std::stoi(salt_hex.substr(i,2),nullptr,16));unsigned char out[32];need(PKCS5_PBKDF2_HMAC(password.data(),password.size(),b.data(),b.size(),iterations,EVP_sha256(),32,out)==1,"Ошибка хеширования",500);return "pbkdf2_sha256$"+std::to_string(iterations)+"$"+salt_hex+"$"+hex(out,32);}
inline bool constant_equal(const std::string&a,const std::string&b){return a.size()==b.size()&&CRYPTO_memcmp(a.data(),b.data(),a.size())==0;}
inline bool password_check(const std::string&p,const std::string&saved){try{std::vector<std::string>parts;std::stringstream ss(saved);std::string x;while(std::getline(ss,x,'$'))parts.push_back(x);if(parts.size()!=4||parts[0]!="pbkdf2_sha256")return false;int it=std::stoi(parts[1]);if(it<100000||it>2000000)return false;return constant_equal(password_hash(p,parts[2],it),saved);}catch(...){return false;}}
inline double num(const json&j,const std::string&key,double fallback=0){auto s=str(j,key);if(s.empty())return fallback;std::replace(s.begin(),s.end(),',','.');size_t used=0;double n;try{n=std::stod(s,&used);}catch(...){throw Error(400,"Поле «"+key+"» должно быть числом");}need(used==s.size()&&std::isfinite(n),"Некорректное число: "+key);return n;}
inline long long integer(const json&j,const std::string&key,long long minimum=0){double n=num(j,key);need(n>=minimum&&std::floor(n)==n&&n<=9007199254740991.0,"Поле «"+key+"» должно быть целым числом не меньше "+std::to_string(minimum));return (long long)n;}
inline Param nullable(const json&j,const std::string&key){auto s=str(j,key);return s.empty()?std::nullopt:Param(s);}
class DB{
 PGconn*c_;
 public:
 DB(){c_=PQconnectdb(env("DATABASE_URL").c_str());if(PQstatus(c_)!=CONNECTION_OK){PQfinish(c_);throw Error(503,"PostgreSQL недоступен");}q("SET TIME ZONE 'UTC'");}
 ~DB(){PQfinish(c_);}DB(const DB&)=delete;DB&operator=(const DB&)=delete;
 json q(const std::string&sql,const std::vector<Param>&args={}){
 std::vector<const char*>a;for(auto&x:args)a.push_back(x?x->c_str():nullptr);
 PGresult*r=PQexecParams(c_,sql.c_str(),a.size(),nullptr,a.data(),nullptr,nullptr,0);
 if(!r)throw Error(503,"Ошибка подключения к PostgreSQL");
 auto status=PQresultStatus(r);
 if(status!=PGRES_TUPLES_OK&&status!=PGRES_COMMAND_OK){std::string code=PQresultErrorField(r,PG_DIAG_SQLSTATE)?PQresultErrorField(r,PG_DIAG_SQLSTATE):"";PQclear(r);if(code=="23505")throw Error(409,"Запись с таким кодом, именем или номером уже существует");if(code=="23503")throw Error(400,"Связанная запись не найдена или используется");if(code=="23514"||code=="22P02"||code=="22003")throw Error(400,"Неверное значение поля");throw Error(500,"Ошибка запроса к базе данных ("+code+")");}
 json rows=json::array();for(int i=0;i<PQntuples(r);i++){json row=json::object();for(int k=0;k<PQnfields(r);k++){auto name=PQfname(r,k);if(PQgetisnull(r,i,k)){row[name]=nullptr;continue;}std::string v=PQgetvalue(r,i,k);auto type=PQftype(r,k);if(type==16)row[name]=v=="t";else if(type==20||type==21||type==23)row[name]=std::stoll(v);else if(type==700||type==701||type==1700)row[name]=std::stod(v);else if(type==114||type==3802)row[name]=json::parse(v);else if(type==1184||type==1114){if(v.size()>10&&v[10]==' ')v[10]='T';if(v.ends_with("+00"))v=v.substr(0,v.size()-3)+"Z";else if(type==1114)v+="Z";row[name]=v;}else row[name]=v;}rows.push_back(row);}PQclear(r);return rows;
 }
 json one(const std::string&s,const std::vector<Param>&a={}){auto r=q(s,a);need(!r.empty(),"Запись не найдена",404);return r[0];}
 void script(const std::string&s){auto*r=PQexec(c_,s.c_str());if(PQresultStatus(r)!=PGRES_COMMAND_OK){std::string e=PQresultErrorMessage(r);PQclear(r);throw std::runtime_error(e);}PQclear(r);}
 void begin(){q("BEGIN");q("SELECT pg_advisory_xact_lock(73482910)");}
 void commit(){q("COMMIT");}void rollback(){q("ROLLBACK");}
};
inline std::vector<std::string>states={"new","used","sharpened","regrind_queue","regrind"};
inline bool available(const std::string&s){return s=="new"||s=="used"||s=="sharpened";}
inline bool allowed(const json&u,const std::string&p){if(str(u,"role")=="ADMIN")return true;auto perms=u.value("permissions_json",json::array());return std::find(perms.begin(),perms.end(),p)!=perms.end();}
inline void permit(const json&u,const std::string&p){need(allowed(u,p),"Недостаточно прав",403);}
inline long long stock(DB&db,const std::string&pid,const std::string&state){return db.one("SELECT qty FROM inventory WHERE product_id=$1 AND state=$2",{pid,state})["qty"].get<long long>();}
inline long long held(DB&db,const json&d){auto r=db.q("SELECT qty FROM holdings WHERE product_id=$1 AND worker_id=$2 AND machine_id=$3 AND state=$4",{str(d,"product_id"),str(d,"worker_id"),str(d,"machine_id"),str(d,"state_from")});return r.empty()?0:r[0]["qty"].get<long long>();}
inline long long usable(DB&db,const std::string&pid){return db.one("SELECT sum(qty)::bigint qty FROM inventory WHERE product_id=$1 AND state IN('new','used','sharpened')",{pid})["qty"].get<long long>();}
void initialize(DB&db);
void import_sqlite(DB&db,const fs::path&source,const fs::path&photo_source,const fs::path&dest);
