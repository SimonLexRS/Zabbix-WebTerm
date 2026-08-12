<?php declare(strict_types = 0);

namespace Modules\WebTerm;

use Zabbix\Core\CModule;

class Module extends CModule {
    public function getName(): string {
        return _('WebTerm - Terminal Web');
    }

    public function getDescription(): string {
        return _('Terminal web SSH/Telnet/Web integrada en Zabbix. Powered by Elitech Solutions.');
    }
}
