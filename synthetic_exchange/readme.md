# 本地协议服务

GET /oai?verb=ListRecords；后续附 resumptionToken=next。命名空间遵循 OAI-PMH 外层，metadata 的 m:record 为本地成果映射。identifier 可能包含冒号，需要稳定映射，deleted header 没有 metadata。日期粒度为天。

场景是响应脚本，不包含采集客户端。可以复制场景文件并改变响应次序、状态码或样本内容来联调。服务只监听 loopback，数据均为合成。

    python3 -m synthetic_exchange.server --port 8765
    python3 -m synthetic_exchange.smoke
