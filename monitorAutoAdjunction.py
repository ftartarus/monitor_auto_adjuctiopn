# coding=utf-8
from django.shortcuts import render
from django.http import HttpResponse,JsonResponse
from django import forms
from django.utils.translation import gettext as _
from django.core.exceptions import ValidationError
from pyzabbix import ZabbixAPI
import xlrd
import json
import numpy as np
import re
import paramiko

#zabbix地址、用户名、密码
#待修改
ZABBIX_SERVER_URL = 'http://192.168.26.221:8880/zabbix'
ZABBIX_USERNAME = 'Admin'
ZABBIX_PASSWORD = 'zabbix'

zapi = ZabbixAPI(ZABBIX_SERVER_URL)
zapi.login(ZABBIX_USERNAME, ZABBIX_PASSWORD)

# proxy_name = 'beijiproxy_10.14.7.67'
# proxy_ip = '192.168.26.221'
# username = 'monitor'
# password = 'u#1d8Dci'
# host_ip = '192.168.26.218'


#接收单条字串: 省份_机房_业务_应用_工程_网元_IP
#返回二维列表: [['省份','机房','业务','应用','工程','网元','IP']]
def receive_individual_data(request):
    #待修改
    individual_data=request.GET.get
    name =[]
    name.append(individual_data.split('_'))
    name,incorrect_name=format_verify(name)
    return name

# 验证excel文件
def validate_excel(value):
    #excel文件格式
    if value.name.split('.')[-1] not in ['xls','xlsx','csv']:
        raise ValidationError(_('Invalid File Type: %(value)s'),params={'value': value})
class UploadExcelForm(forms.Form):
    excel = forms.FileField(validators=[validate_excel]) #这里使用自定义的验证

#前端提交excel文件，批量导入host
# 返回json:{'correct_res':[[]],
#           'incorrect_res':[[]]}
#元素为二维数组
#如果不是'xls','xlsx','csv'文件则返回错误信息字符串
def import_excel(request):
    #读取excel
    if request.method == 'POST':  # 当提交表单时
        form = UploadExcelForm(request.POST, request.FILES)
        data=[]
        if form.is_valid():
            wb = xlrd.open_workbook(filename=None, file_contents=request.FILES['excel'].read())
            table = wb.sheets()[0]
            row = table.nrows
            for i in range(1, row):
                col = table.row_values(i)
                if set(col) != {''} and col != []:
                    data.append(col)

            #返回前台json
            correct_res, incorrect_res = format_verify(data)
            res = {
                'correct_res': correct_res,
                'incorrect_res': incorrect_res,
                'template_res': [],
                'group_res': [],
                'host_res': [],
                'proxy_res': [],
                'agent_res':[],
            }

            # 调用函数
            for name in correct_res:
                #主机是否存在
                if_host_exist = host_exist(name)
                #验证要加群组模板是否已经存在
                template_group_verify(name)
                #主机存在，关联模板，纳入群组
                if if_host_exist == 1:
                    template_res = template_update(name)
                    group_res = group_update(name)
                    res['template_res'].append('_'.join(name) + ':' + template_res)
                    res['group_res'].append('_'.join(name) + ':' + group_res)
                #主机不存在
                else:
                    #选择proxy
                    proxy_name,username,password=proxy_select(name)
                    host_ip=name[-1]
                    proxy_ip=proxy_name.split('_')[-1]
                    #测试proxy，host网络
                    proxy_connect_res = proxy_host_ssh(proxy_ip, host_ip, username, password)
                    if proxy_connect_res == 'proxy网络连接正常！':
                        #创建主机
                        host_res = host_create(name)
                        res['host_res'].append('_'.join(name) + ':' + host_res)
                        #关联proxy
                        proxy_res = proxy_update(name, proxy_name)
                        res['proxy_res'].append('_'.join(name) + ':' + proxy_res)
                        #部署Agent
                        agent_res=deploy_agent()
                        res['agent_res'].append('_'.join(name) + ':' + agent_res)
                    else:
                        res['proxy_res'].append('_'.join(name) + ':' + proxy_connect_res)
            return HttpResponse(json.dumps(res, ensure_ascii=False), content_type="application/json")
        else:
            alert_str='请导入正确格式文件!'
            return HttpResponse(alert_str)
    #测试页面用
    return render(request, 'home.html')

#验证excel数据格式
# 传入参数：
#二维数组：
#返回值：
# 两个二维数组：正确格式数据，不正确格式数据
def format_verify(data):
    # 待修改
    verify_dict=[
        ['省份1', '省份2', '省份3'],
        ['机房1', '机房2', '机房3'],
        ['业务1', '业务2', '业务3'],
        ['应用1', '应用2', '应用3'],
        ['工程1', '工程2', '工程3'],
        ['网元1', '网元2', '网元3'],
    ]
    incorrect_data = []
    data1=data.copy()
    for i in data1:
        bool_res=[]
        ip_split = i[-1].split('.')
        for j in range(len(verify_dict)):
            bool_res.append(i[j] in verify_dict[j])
        for s in ip_split:
            if re.match('^[0-9]+$',s):
                bool_res.append(True)
            else:
                bool_res.append(False)
        if not (np.all(bool_res) and len(ip_split)==4):
            incorrect_data.append(data.pop(data.index(i)))
    return data,incorrect_data


#检测主机是否存在
#传入参数：
#name:数组，例：['省份','机房','业务','应用','工程','网元','IP']
#返回值：
#存在返回1,不存在返回0,若主机存在，则直接将其设置为已监控状态
def host_exist(name):
    host_name = name[-1]
    host_id=zapi.host.get(filter={'host':host_name})
    if host_id:
        zapi.host.update(
            {
                "hostid": host_id[0]['hostid'],
                "status": 0
            }
        )
        return 1
    else:
        return 0


#组群、模板关联规则，若不存在则创建
def template_group_verify(name):
    # 待修改
    groups_name , templates_name = association_rules(name)
    for group_name in groups_name:
        group_id = zapi.hostgroup.get(filter={'name': group_name})
        if not group_id:
            zapi.hostgroup.create(name=group_name)
    for template_name in templates_name:
        groups_id = zapi.hostgroup.get(filter={'name': groups_name})
        template_id = zapi.template.get(filter={'name': template_name})
        if not template_id:
            #待修改
            template_new=zapi.template.get(filter={'name': 'Template os linux'})
            zapi.template.create(host=template_new[0]['host']+' '+name[-1],
                                 name=template_name,
                                 groups=groups_id,
                                 templates=template_new)


#更新模板
#返回值：
#模板关联是否成功 字段
def template_update(name):
    host_name = name[-1]
    # 待修改
    templates_name = ['_'.join(['Template os linux',name[0]])]
    host_id = zapi.host.get(filter={'host': host_name})
    templates_id=zapi.template.get(filter={'name':templates_name})
    templates_to_add=[ m['templateid'] for m in templates_id]
    res = zapi.template.get(output=["templateid"],selectParentTemplates="refer",hostids=int(host_id[0]['hostid']))
    templates_old = [ m['templateid'] for m in res]
    templates_new = list(set(templates_to_add).union(set(templates_old)))
    templates_new_format = [{'templateid': m} for m in templates_new]
    try:
        zapi.host.update(hostid=int(host_id[0]['hostid']), templates=templates_new_format)
        return '模板关联成功!'
    except:
        return '模板关联失败!'


#纳入组群
#返回值：
#组群是否纳入成功 字段
def group_update(name):
    host_name = name[-1]
    # 待修改
    groups_name, templates_name = association_rules(name)
    # 待修改
    host_id = zapi.host.get(filter={'host': host_name})
    groups_id = zapi.hostgroup.get(filter={'name': groups_name})
    groups_to_add = [m['groupid'] for m in groups_id]
    res = zapi.hostgroup.get(output=["groupid"], hostids=int(host_id[0]['hostid']))
    groups_old = [m['groupid'] for m in res]
    groups_new = list(set(groups_to_add).union(set(groups_old)))
    groups_new_format = [{'groupid': m} for m in groups_new]
    try:
        zapi.host.update(hostid=int(host_id[0]['hostid']), groups=groups_new_format)
        return '组群纳入成功!'
    except:
        return '组群纳入失败！'


#创建主机并纳入主机群组、关联模板
#返回值：
#主机是否添加成功 字段
def host_create(name):
    host_name=name[-1]
    ip=name[-1]
    # 待修改
    groups_name, templates_name = association_rules(name)
    groups_id = zapi.hostgroup.get(filter={'name': groups_name})
    templates_id = zapi.template.get(filter={'name':templates_name})
    try:
        zapi.host.create(
            {
                "host": host_name,
                "groups": groups_id,
                "templates": templates_id,
                'name':name,
                "interfaces": [
                    {
                        "type": 1,
                        "main": 1,
                        "useip": 1,
                        "ip": ip,
                        "dns": "",
                        "port": "9050"
                    }
                ],
                "status": 0
            }
        )
        return "主机添加成功!"
    except:
        return "主机添加失败!"


#检测proxy到主机网络通信
#传入参数：proxyIP，hostIP，proxy用户名，proxy密码
#良好返回1，无连接返回0，未知返回2
def proxy_host_ssh(proxy_ip,host_ip,username,password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(hostname=proxy_ip, username=username, password=password)
        stdin, stdout, stderr = ssh.exec_command('ping '+host_ip+' -c 4')
        if len(stdout.read().split('\n')) == 10:
            return 'proxy网络连接正常！'
        else:
            return 'proxy网络连接失败！'
    except:
        return 'proxy无法登陆！'


#更新proxy
#传入参数：host可见名，proxy主机名
def proxy_update(name,proxy_name):
    host_name = name[-1]
    host_id = zapi.host.get(filter={'host': host_name})
    proxy_hostid=int(zapi.proxy.get(filter={'host':proxy_name})[0]['proxyid'])
    try:
        zapi.host.update(hostid=int(host_id[0]['hostid']), proxy_hostid=proxy_hostid)
        return 'proxy添加成功！'
    except:
        return 'proxy添加失败！'


#群组、模板名关联规则
#返回组群名，模板名，数组格式，内部元素为字符串
#例：['省份_机房_业务_应用_主机群组','省份_机房_业务_应用_主机群组']
def association_rules(name):
    groups_name = ['_'.join([name[0], name[1], '主机群组']),
                   '_'.join([name[0], name[1], name[2], name[3], name[4], '主机群组'])]
    # 待修改
    templates_name = ['_'.join(['Template os linux', name[0]])]
    return groups_name,templates_name


#省份proxy对照
#根据省份返回proxy name，用户名，密码
def proxy_select(name):
    #待修改
    proxy_dict={}

    proxy_name=proxy_dict[name[0]]
    username=
    password=
    return proxy_name,username,password


#部署Agent
def deploy_agent():
    try:

        return 'Agent部署成功！'
    except:
        return 'Agent部署失败！'