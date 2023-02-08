# -*- coding: utf-8 -*-

# @Author  : wzdnzd
# @Time    : 2022-07-15

import json
import os
import subprocess
from dataclasses import dataclass

import renewal
from airport import AirPort
from logger import logger
from origin import Origin
from push import PushTo


@dataclass
class TaskConfig:
    # 任务名
    name: str

    # subconverter程序名
    bin_name: str

    # 网址域名
    domain: str = ""

    # 订阅地址
    sub: str = ""

    # 任务编号
    index: int = 1

    # 失败重试次数
    retry: int = 3

    # 最高允许倍率
    rate: float = 3.0

    # 标签
    tag: str = ""

    # 套餐续期配置
    renew: dict = None

    # 节点重命名规则
    rename: str = ""

    # 节点排除规则
    exclude: str = ""
    include: str = ""

    # 是否检测节点存活状态
    liveness: bool = True

    # skip-cert-verify
    allow_insecure: bool = False

    # 覆盖subconverter默认exclude规则
    ignorede: bool = False


def execute(task_conf: TaskConfig) -> list:
    if not task_conf:
        return []

    obj = AirPort(
        name=task_conf.name,
        site=task_conf.domain,
        sub=task_conf.sub,
        rename=task_conf.rename,
        exclude=task_conf.exclude,
        include=task_conf.include,
        liveness=task_conf.liveness,
    )

    logger.info(f"start fetch proxy: name=[{task_conf.name}]\tdomain=[{obj.ref}]")

    # 套餐续期
    if task_conf.renew:
        sub_url = renewal.add_traffic_flow(domain=obj.ref, params=task_conf.renew)
        if sub_url and not obj.registed:
            obj.registed = True
            obj.sub = sub_url

    cookie, authorization = obj.get_subscribe(retry=task_conf.retry)
    proxies = obj.parse(
        cookie=cookie,
        auth=authorization,
        retry=task_conf.retry,
        rate=task_conf.rate,
        bin_name=task_conf.bin_name,
        tag=task_conf.tag,
        allow_insecure=task_conf.allow_insecure,
        ignore_exclude=task_conf.ignorede,
    )

    logger.info(
        f"finished fetch proxy: name=[{task_conf.name}]\tdomain=[{obj.ref}]\tcount=[{len(proxies)}]"
    )

    return proxies


def liveness_fillter(proxies: list) -> tuple[list, list]:
    if not list:
        return [], []

    checks, nochecks = [], []
    for p in proxies:
        if not isinstance(p, dict):
            continue

        liveness = p.pop("liveness", True)
        if liveness:
            checks.append(p)
        else:
            p.pop("sub", "")
            nochecks.append(p)

    return checks, nochecks


def cleanup(
    process: subprocess.Popen, filepath: str = "", filenames: list = []
) -> None:
    process.terminate()

    if not filepath or not filenames:
        return

    for name in filenames:
        filename = os.path.join(filepath, name)
        if os.path.exists(filename):
            os.remove(filename)


def dedup_task(tasks: list) -> list:
    if not tasks:
        return []
    items = []
    for task in tasks:
        if not isinstance(task, TaskConfig):
            logger.error(
                f"[DedupError] need type 'TaskConfig' but got type '{type(task)}'"
            )
            continue

        found = False
        for item in items:
            if task.sub != "":
                if task.sub == item.sub:
                    found = True
            else:
                if task.domain == item.domain and task.index == item.index:
                    found = True

            if found:
                if not item.rename:
                    item.rename = task.rename
                if task.exclude:
                    item.exclude = "|".join([item.exclude, task.exclude]).removeprefix(
                        "|"
                    )
                if task.include:
                    item.include = "|".join([item.include, task.include]).removeprefix(
                        "|"
                    )

                break

        if not found:
            items.append(task)

    return items


def merge_config(configs: list) -> list:
    def judge_exists(raw: dict, target: dict) -> bool:
        if not raw or not target:
            return False

        rsub = raw.get("sub").strip()
        tsub = target.get("sub", "")
        if not tsub:
            if rsub:
                return False
            return raw.get("domain", "").strip() == target.get("domain", "").strip()
        if isinstance(tsub, str):
            return rsub == tsub.strip()
        for sub in tsub:
            if rsub == sub.strip():
                return True
        return False

    if not configs:
        return []
    items = []
    for conf in configs:
        if not isinstance(conf, dict):
            logger.error(f"[MergeError] need type 'dict' but got type '{type(conf)}'")
            continue

        sub = conf.get("sub", "")
        if isinstance(sub, list) and len(sub) <= 1:
            sub = sub[0] if sub else ""

        # 人工维护配置，无需合并
        if isinstance(sub, list) or conf.get("renew", {}):
            items.append(conf)
            continue

        found = False
        conf["sub"] = sub
        for item in items:
            found = judge_exists(raw=conf, target=item)
            if found:
                if conf.get("errors", 0) > item.get("errors", 0):
                    item["errors"] = conf.get("errors", 0)
                if item.get("debut", False):
                    item["debut"] = conf.get("debut", False)
                if not item.get("rename", ""):
                    item["rename"] = conf.get("rename", "")
                if conf.get("exclude", ""):
                    item["exclude"] = "|".join(
                        [item.get("exclude", ""), conf.get("exclude", "")]
                    ).removeprefix("|")
                if conf.get("include", ""):
                    item["include"] = "|".join(
                        [item.get("include", ""), conf.get("include", "")]
                    ).removeprefix("|")

                break

        if not found:
            items.append(conf)

    return items


def refresh(config: dict, push: PushTo, alives: dict, filepath: str = "") -> None:
    if not config or not push:
        logger.error(
            "[UpdateError] cannot update remote config because content is empty"
        )
        return

    update_conf = config.get("update", {})
    if not update_conf.get("enable", False):
        logger.debug("[UpdateError] skip update remote config because enable=[False]")
        return

    if not push.validate(push_conf=update_conf):
        logger.error(f"[UpdateError] update config is invalidate")
        return

    domains = merge_config(configs=config.get("domains", []))
    if alives:
        sites = []
        for item in domains:
            source = item.get("origin", "")
            sub = item.get("sub", "")
            if isinstance(sub, list) and len(sub) <= 1:
                sub = sub[0] if sub else ""
            if (
                source in [Origin.TEMPORARY.name, Origin.OWNED.name]
                or isinstance(sub, list)
                or alives.get(sub, False)
            ):
                item.pop("errors", None)
                item.pop("debut", None)
                sites.append(item)
                continue

            errors = item.get("errors", 1)
            expire = Origin.get_expire(source)
            if errors < expire and not item.get("debut", False):
                item.pop("debut", None)
                sites.append(item)

        config["domains"] = sites
        domains = config.get("domains", [])

    if not domains:
        logger.error("[UpdateError] skip update remote config because domians is empty")
        return

    content = json.dumps(config)
    if filepath:
        directory = os.path.abspath(os.path.dirname(filepath))
        os.makedirs(directory, exist_ok=True)
        with open(filepath, "w+", encoding="UTF8") as f:
            f.write(content)
            f.flush()

    push.push_to(content=content, push_conf=update_conf, group="update")
