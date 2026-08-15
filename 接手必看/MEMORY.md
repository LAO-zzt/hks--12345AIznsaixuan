雪茄扒图项目:
- Excel解析: 品牌标题行混在产品行中, 按关键词+长度<50过滤; "烟 丝 系 列"含空格需strip后匹配
- TimeCigar(非Shopify): AJAX /pages/product/ajax_load_product_list.php?brand=X&page=Y (brand: 18大卫杜夫烟丝,9雪茄,150法官,181马坝,141SGB,143拉特雷,157拉森); JSON-LD含description/image; 原图URL去/150px/段和_150px后缀; URL空格需urllib.parse.quote()
- 70Cigars(Shopify): /search/suggest.json?q=...&resources[type]=product, 详情/products/{handle}.json, tags需split(','), 图无水印1-3张; "马杜罗"优选Maduro否则Natural; 泛关键词需带具体编号精确匹配
- cohcigars: search返417但products.json可用; HYH Puro水印不可用; PipeUncle Cloudflare拦截
- 验证: 产品名含"烟丝"但tags含cigars或英文名为雪茄型号(Robusto/Toro)则错误匹配; 下载后验证文件大小>0
§
- Windows环境注意: cmd下Python输出中文需sys.stdout.reconfigure(encoding='utf-8')否则乱码; 用户名含单引号(zhong'zi'tao)致OpenCV imread失败需cp到workspace再处理
§
caribbean1873官网+H5+GitHub+服务器: Next.js/React深绿金色风格, 年龄验证, 子品牌(濑龙/加勒比/琥珀1873,汇美全球购,匯康藥房); H5商城shop.caribbean1873.com独立部署, 无在线支付, 展示+到店自提(澳门实体店); QR解码: 红底二维码需PIL ImageEnhance.Contrast(2.0)增强+convert('L')后pyzbar解码; GitHub: LAO-zzt, 官网仓库私有(https://github.com/LAO-zzt/-1873-); 服务器腾讯云港Ubuntu IP 43.129.213.238
§
GitHub: LAO-zzt, 官网仓库私有(https://github.com/LAO-zzt/-1873-); 服务器腾讯云港Ubuntu IP 43.129.213.238
§
视频制作经验与约束: 即梦/可灵积分有限, 展示/演示优先LLM+图片, 视频用预录屏或成品代替现场生成; 空间一致性: 即梦/混元/可灵画布均无法从单图生成同空间多角度, 当前方案=单参考图+文字控角度, 混元3D世界模型2.0全量开放后切换, 最优选=实地拍多角度照片; 本地视频可通过浏览器打开后用builtin_browser screenshot逐帧查看(tabs_context→screenshot→wait循环)
§
琥珀皇朝AI短剧: 虚拟IP"琥珀皇朝,今晚你是皇", 品牌Ambe Karaoke & Bar(澳门KTV/酒吧, 波尔图街477号); 竖屏9:16抖音+小红书90秒/集; 社交预热(未开业), 4个待茄师角色; 制作: 即梦Seedream5.0Pro(图)+Seedance2.0mini(视频)+剪映(后期), 人物卡A版定稿; 项目结构=设定.md+短剧脚本/+场景物料/+门店动线空间设计/+人物卡/+项目背景/; 配套AI skill生成脚本分镜提示词
§
顺德黑客松: 与蔡亲信组队, 赛道企业解决方案(第一)+自由创新(第二)
- 参赛作品定位: "线下实体店冷启动解决方案"五层架构: 诊断→策略→生产→分发→复盘; 琥珀皇朝是第一个落地案例
- 比赛叙事: "每年百万实体店开业日粉丝为零,我们做了一套AI冷启动系统"
- 仓库hackathon-base(Desktop\规划\): FastAPI+DeepSeek+飞书底座, 业务集中app/business/prompt_template.py; 赛题8/14晚公布, 按docs/agent/角色提示词.md第10节先回合制对齐出brief才准写代码; 分工: WorkBuddy拆题定方向+写基础代码, 我(QoderWork)写架构+评审优化代码
- 注意: hackathon-base内含ppt-master(约705MB), 递归列举/搜索会超时, 用浅层列举(-Depth 1)或排除ppt-master
§
"茄语"项目: FastAPI+双Bot(热点+视频)+8角色绑定3店铺IP+飞书文档API+保活系统, 部署CloudStudio端口18888
§
E01高频事件预警处置系统(顺德黑客松赛题): 代码Desktop\多频工单识别, git逐阶段留痕; 真实数据全量接入(12.8万条19s, 缓存0.8s); 工单编号前6位YYMMDD仅日级(展示勿伪造时分), 标题即事件标签, >1.5万行自动切标题规则分组(DBSCAN O(n²)不可行); 聚类调优=同义词归一+实体加权拼接(事件权重最高)+同主体碎片簇归并+签名回收噪声点; 风险评分=相对频次基数+大体量(≥max10%)高关注保底; 处置建议=规则词典优先+DeepSeek兜底(Top100覆盖100%); 密钥=webapp/secrets.json(gitignore), 取值链: 页面>环境变量DEEPSEEK_API_KEY>文件(用户Key已暴露,赛后需提醒重置); 自研web=webapp/server.py(FastAPI)+static/index.html深色大屏, 端口8600, Streamlit 8501备用; 数据文件data/input由/api/datafiles列出, 链接只记README不进代码; 第一屏V2.1产品化已完成(1957b8e); 待做: 睿评证据链
§
Streamlit坑汇总: 无头UI验证用官方AppTest(from_file+button.click().run()); metric值是字符串; 图表坑: st.bar_chart缩放标签遮挡→改plotly(x类目轴+automargin+tickangle=-45, 分类用横向bar); plotly>=6弃scatter_mapbox→px.scatter_map(MapLibre), 瓦片需联网; 1.61废弃use_container_width→width="stretch"; 长跑进程缓存本地模块ImportError, watcher不可靠须taskkill重启; 多页= session_state["page"]+按钮导航+st.rerun(), 改已实例化widget同名key抛StreamlitAPIException
§
ECharts离线化: npmmirror下载echarts.min.js到static/vendor/; display:none容器里echarts.init尺寸为0, 须先切页可见再渲染图表
§
金山文档kdocs分享链接: 匿名访问302跳SSO登录; 用户浏览器已登录会话可读; drive.kdocs.cn/api/v5/links/{sid}查文件元数据; 程序拉取需登录Cookie(会过期),比赛场景让用户手动下载xlsx放本地最稳
§
Windows进程/端口管理坑: Git Bash下taskkill /PID被解析为路径, 需cmd //c "taskkill /F /PID x"; taskkill父PID后子进程仍占端口, 须netstat -ano找实际LISTENING PID再杀; 代码改动后须手动清result_*.pkl结果缓存(缓存键不含代码版本, 改代码/词典后验证前必须清缓存否则复跑旧结果)