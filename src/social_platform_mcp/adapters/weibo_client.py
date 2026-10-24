"""微博 CLI 进程、JSON 解码与安全错误归一化。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from social_platform_mcp.contracts import (
    PlatformCallBudget,
    PlatformError,
    PlatformErrorType,
    PlatformName,
    normalize_required_text,
)
from social_platform_mcp.operations import OperationUncertainty


_MAX_COMMAND_OUTPUT_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], float], Awaitable[CommandResult]]


async def run_command(arguments: Sequence[str], timeout: float) -> CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise PlatformError(
            platform=PlatformName.WEIBO,
            error_type=PlatformErrorType.UNAVAILABLE,
            message="微博 CLI 不可用。",
        ) from exc
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise PlatformError(
            platform=PlatformName.WEIBO,
            error_type=PlatformErrorType.TIMEOUT,
            message="微博请求超时。",
            retryable=True,
        ) from exc
    if len(stdout) > _MAX_COMMAND_OUTPUT_BYTES or len(stderr) > _MAX_COMMAND_OUTPUT_BYTES:
        raise PlatformError(
            platform=PlatformName.WEIBO,
            error_type=PlatformErrorType.INVALID_RESPONSE,
            message="微博 CLI 返回内容超过安全上限。",
        )
    return CommandResult(
        returncode=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


class WeiboCliClient:
    def __init__(
        self,
        *,
        cli_path: str,
        budget: PlatformCallBudget,
        runner: CommandRunner | None = None,
    ) -> None:
        self._cli_path = normalize_required_text(
            cli_path,
            name="Weibo CLI path",
            max_length=1_024,
        )
        self._budget = budget
        self._runner = runner or run_command

    async def invoke(self, *arguments: str) -> Any:
        result = await self._runner(
            (self._cli_path, *arguments, "--output", "json"),
            self._budget.timeout_seconds,
        )
        if result.returncode != 0:
            self._raise_cli_error(result.stderr or result.stdout)
        try:
            return json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise PlatformError(
                platform=PlatformName.WEIBO,
                error_type=PlatformErrorType.INVALID_RESPONSE,
                message="微博 CLI 返回了无效 JSON。",
            ) from exc

    async def invoke_write(
        self,
        *arguments: str,
    ) -> tuple[Any | None, OperationUncertainty | None]:
        try:
            return await self.invoke(*arguments), None
        except PlatformError as exc:
            reasons = {
                PlatformErrorType.TIMEOUT: OperationUncertainty.TIMEOUT,
                PlatformErrorType.UNAVAILABLE: OperationUncertainty.UNAVAILABLE,
                PlatformErrorType.INVALID_RESPONSE: OperationUncertainty.INVALID_RESPONSE,
            }
            reason = reasons.get(exc.error_type)
            if reason is None:
                raise
            return None, reason

    @staticmethod
    def _raise_cli_error(raw_message: str) -> None:
        message = raw_message.casefold()
        if any(
            word in message
            for word in (
                "登录",
                "login",
                "token expired",
                "invalid token",
                "missing token",
                "401",
            )
        ):
            error_type = PlatformErrorType.NOT_AUTHENTICATED
            safe_message = "微博账号未登录或登录已失效。"
            retryable = False
        elif any(
            word in message
            for word in ("额度", "quota", "rate limit", "429", "credits")
        ):
            error_type = PlatformErrorType.RATE_LIMITED
            safe_message = "微博接口额度不足或请求频率受限。"
            retryable = True
        elif any(
            word in message
            for word in ("permission", "forbidden", "无权限", "权限不足", "403")
        ):
            error_type = PlatformErrorType.PERMISSION_DENIED
            safe_message = "当前微博账号没有执行该操作的权限。"
            retryable = False
        elif any(
            word in message
            for word in ("invalid argument", "invalid parameter", "参数错误", "400")
        ):
            error_type = PlatformErrorType.INVALID_REQUEST
            safe_message = "微博请求参数未被平台接受。"
            retryable = False
        elif any(word in message for word in ("timeout", "timed out", "超时")):
            error_type = PlatformErrorType.TIMEOUT
            safe_message = "微博请求超时。"
            retryable = True
        else:
            error_type = PlatformErrorType.UNAVAILABLE
            safe_message = "微博 CLI 当前不可用。"
            retryable = True
        raise PlatformError(
            platform=PlatformName.WEIBO,
            error_type=error_type,
            message=safe_message,
            retryable=retryable,
        )
