<?php declare(strict_types = 0);

namespace Modules\WebTerm\Actions;

use API;
use CController;
use CControllerResponseData;

class Connect extends CController {
    protected function init(): void {
        // CSRF validation enabled for security
    }

    protected function checkInput(): bool {
        return $this->validateInput([
            'hostid' => 'required|id',
            'mode'   => 'string'
        ]);
    }

    protected function checkPermissions(): bool {
        return $this->getUserType() >= USER_TYPE_ZABBIX_ADMIN;
    }

    protected function doAction(): void {
        $hostid = $this->getInput('hostid');
        $mode = $this->getInput('mode', 'ssh');

        $hosts = API::Host()->get([
            'output' => ['hostid', 'name', 'host'],
            'selectInterfaces' => ['interfaceid', 'ip', 'dns', 'port', 'type', 'main'],
            'hostids' => [$hostid]
        ]);

        $host = $hosts ? $hosts[0] : null;
        $ip = '';
        if ($host && !empty($host['interfaces'])) {
            foreach ($host['interfaces'] as $iface) {
                if ($iface['main'] == 1) {
                    $ip = !empty($iface['ip']) ? $iface['ip'] : $iface['dns'];
                    break;
                }
            }
            if (!$ip) {
                $ip = !empty($host['interfaces'][0]['ip'])
                    ? $host['interfaces'][0]['ip']
                    : $host['interfaces'][0]['dns'];
            }
        }

        $this->setResponse(new CControllerResponseData([
            'host' => $host,
            'ip' => $ip,
            'hostid' => $hostid,
            'mode' => $mode
        ]));
    }
}
