import logging
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from app.core.config import settings

logger = logging.getLogger("AdminRouter")
router = APIRouter(tags=["Admin Panel"])

ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru" class="h-full bg-slate-900 text-slate-100">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI Manager SaaS — Панель Управления amoCRM</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <style>
    [x-cloak] { display: none !important; }
  </style>
</head>
<body class="h-full antialiased font-sans flex flex-col" x-data="adminApp()" x-init="init()">
  <!-- Header -->
  <header class="border-b border-slate-800 bg-slate-900/80 backdrop-blur sticky top-0 z-30 px-6 py-4 flex items-center justify-between">
    <div class="flex items-center space-x-3">
      <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-indigo-600 to-violet-500 flex items-center justify-center text-white shadow-lg shadow-indigo-500/30">
        <i class="fa-solid fa-robot text-lg"></i>
      </div>
      <div>
        <h1 class="font-bold text-lg leading-tight flex items-center gap-2">
          AI Manager SaaS
          <span class="text-xs font-medium px-2 py-0.5 rounded-full bg-indigo-500/20 text-indigo-400 border border-indigo-500/30">amoCRM</span>
        </h1>
        <p class="text-xs text-slate-400">Управление мультитенантными подключениями и Salesbot</p>
      </div>
    </div>
    <div class="flex items-center space-x-3">
      <a href="/docs" target="_blank" class="text-xs text-slate-400 hover:text-white px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 transition flex items-center gap-2">
        <i class="fa-solid fa-book"></i> Swagger Docs
      </a>
      <button @click="fetchAccounts()" class="text-xs text-slate-400 hover:text-white px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 transition flex items-center gap-2">
        <i class="fa-solid fa-rotate" :class="loading ? 'animate-spin' : ''"></i> Обновить
      </button>
      <button @click="openAddModal = true" class="text-xs font-semibold px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/30 transition flex items-center gap-2">
        <i class="fa-solid fa-plus"></i> Подключить аккаунт
      </button>
    </div>
  </header>

  <!-- Main content -->
  <main class="flex-1 max-w-7xl w-full mx-auto p-6 space-y-6">
    <!-- Notifications banner / toast -->
    <div x-show="toast.show" x-transition x-cloak class="fixed bottom-6 right-6 z-50 max-w-md px-4 py-3 rounded-xl shadow-2xl flex items-center space-x-3 border"
         :class="toast.type === 'error' ? 'bg-red-950/90 border-red-800 text-red-200' : 'bg-emerald-950/90 border-emerald-800 text-emerald-200'">
      <i class="fa-solid" :class="toast.type === 'error' ? 'fa-circle-exclamation' : 'fa-circle-check'"></i>
      <span class="text-sm font-medium" x-text="toast.message"></span>
      <button @click="toast.show = false" class="ml-auto text-xs opacity-60 hover:opacity-100">&times;</button>
    </div>

    <!-- Stats Bar -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-4">
        <div class="text-xs font-medium text-slate-400">Подключенных аккаунтов</div>
        <div class="text-2xl font-bold mt-1 text-white" x-text="accounts.length"></div>
      </div>
      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-4">
        <div class="text-xs font-medium text-slate-400">Верифицированные вебхуки</div>
        <div class="text-2xl font-bold mt-1 text-emerald-400" x-text="accounts.filter(a => a.webhook_verified).length"></div>
      </div>
      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-4">
        <div class="text-xs font-medium text-slate-400">Привязанных Salesbot</div>
        <div class="text-2xl font-bold mt-1 text-indigo-400" x-text="accounts.filter(a => a.bot_id).length"></div>
      </div>
      <div class="bg-slate-800/60 border border-slate-700/60 rounded-2xl p-4">
        <div class="text-xs font-medium text-slate-400">Статус сервиса</div>
        <div class="text-lg font-bold mt-1 text-emerald-400 flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse"></span> Онлайн
        </div>
      </div>
    </div>

    <!-- Accounts List -->
    <div class="bg-slate-800/40 border border-slate-800 rounded-2xl overflow-hidden shadow-xl">
      <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between">
        <h2 class="font-semibold text-white flex items-center gap-2">
          <i class="fa-solid fa-building text-indigo-400"></i> Подключенные аккаунты amoCRM
        </h2>
        <span class="text-xs text-slate-400">Мультитенантная изоляция настроек и сделок</span>
      </div>

      <template x-if="accounts.length === 0 && !loading">
        <div class="p-12 text-center text-slate-400 space-y-3">
          <div class="w-12 h-12 mx-auto rounded-full bg-slate-800 flex items-center justify-center text-slate-500 text-xl">
            <i class="fa-solid fa-inbox"></i>
          </div>
          <p class="text-sm">Нет подключенных аккаунтов amoCRM.</p>
          <button @click="openAddModal = true" class="text-xs font-medium px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white">
            Подключить первый аккаунт
          </button>
        </div>
      </template>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm" x-show="accounts.length > 0">
          <thead class="bg-slate-800/70 text-slate-400 text-xs uppercase font-semibold">
            <tr>
              <th class="px-6 py-3">Аккаунт / Поддомен</th>
              <th class="px-6 py-3">Статус онбординга</th>
              <th class="px-6 py-3">Поле ответа ИИ</th>
              <th class="px-6 py-3">Salesbot</th>
              <th class="px-6 py-3">Вебхук</th>
              <th class="px-6 py-3 text-right whitespace-nowrap">Действия</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-800/80">
            <template x-for="acc in accounts" :key="acc.id">
              <tr class="hover:bg-slate-800/30 transition" :class="acc.is_active ? '' : 'bg-rose-950/10 opacity-75'">
                <td class="px-6 py-4">
                  <div class="flex items-center gap-2">
                    <div class="font-medium text-white" x-text="acc.name || acc.subdomain"></div>
                    <span x-show="acc.is_active" class="text-[10px] font-bold px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30">Активен</span>
                    <span x-show="!acc.is_active" class="text-[10px] font-bold px-1.5 py-0.5 rounded bg-rose-500/20 text-rose-400 border border-rose-500/30">Остановлен</span>
                  </div>
                  <div class="text-xs text-indigo-400 flex items-center gap-1 mt-0.5">
                    <span x-text="acc.subdomain"></span>.amocrm.ru
                    <a :href="'https://' + acc.subdomain + '.amocrm.ru'" target="_blank" class="hover:text-white">
                      <i class="fa-solid fa-arrow-up-right-from-square text-[10px]"></i>
                    </a>
                  </div>
                </td>
                <td class="px-6 py-4">
                  <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium"
                        :class="{
                          'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30': acc.status === 'verified',
                          'bg-blue-500/10 text-blue-400 border border-blue-500/30': acc.status === 'configured' || acc.status === 'bot_linked',
                          'bg-amber-500/10 text-amber-400 border border-amber-500/30': acc.status === 'awaiting_manual_bot' || acc.status === 'field_created',
                          'bg-rose-500/10 text-rose-400 border border-rose-500/30': acc.status === 'error'
                        }">
                    <span class="w-1.5 h-1.5 rounded-full"
                          :class="{
                            'bg-emerald-400': acc.status === 'verified',
                            'bg-blue-400': acc.status === 'configured' || acc.status === 'bot_linked',
                            'bg-amber-400': acc.status === 'awaiting_manual_bot' || acc.status === 'field_created',
                            'bg-rose-400': acc.status === 'error'
                          }"></span>
                    <span x-text="formatStatus(acc.status, acc.last_error)"></span>
                  </span>
                </td>
                <td class="px-6 py-4 text-xs font-mono text-slate-300">
                  <span x-text="acc.ai_reply_field_id ? '#' + acc.ai_reply_field_id : '—'"></span>
                </td>
                <td class="px-6 py-4 text-xs font-mono">
                  <span x-show="acc.bot_id" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">
                    <i class="fa-solid fa-robot text-[10px]"></i>
                    <span x-text="'Bot #' + acc.bot_id"></span>
                  </span>
                  <span x-show="!acc.bot_id" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
                    <i class="fa-solid fa-circle-exclamation text-[10px]"></i>
                    <span>Не привязан</span>
                  </span>
                </td>
                <td class="px-6 py-4 text-xs">
                  <span x-show="acc.webhook_verified" class="text-emerald-400 flex items-center gap-1">
                    <i class="fa-solid fa-check"></i> Верифицирован
                  </span>
                  <span x-show="!acc.webhook_verified" class="text-amber-400 flex items-center gap-1">
                    <i class="fa-solid fa-clock"></i> Ожидает тест
                  </span>
                </td>
                <td class="px-6 py-4 whitespace-nowrap text-right">
                  <div class="flex items-center justify-end gap-2">
                    <button @click="toggleAccountActive(acc)"
                            class="w-28 h-8 rounded-lg text-xs font-semibold transition inline-flex items-center justify-center gap-1.5 shadow-md"
                            :class="acc.is_active ? 'bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/30' : 'bg-rose-600 hover:bg-rose-500 text-white shadow-rose-600/30'"
                            :title="acc.is_active ? 'Нажмите, чтобы остановить аккаунт' : 'Нажмите, чтобы запустить аккаунт'">
                      <i class="fa-solid text-[11px]" :class="acc.is_active ? 'fa-play' : 'fa-stop'"></i>
                      <span x-text="acc.is_active ? 'Старт' : 'Стоп'"></span>
                    </button>
                    <button @click="openManageModal(acc)"
                            class="w-28 h-8 rounded-lg bg-slate-700 hover:bg-slate-600 text-xs font-medium transition text-white inline-flex items-center justify-center gap-1.5">
                      <i class="fa-solid fa-gear text-[11px]"></i>
                      <span>Настроить</span>
                    </button>
                    <button @click="testBot(acc.id)"
                            class="w-28 h-8 rounded-lg bg-indigo-600/30 hover:bg-indigo-600 text-indigo-300 hover:text-white border border-indigo-500/30 text-xs font-medium transition inline-flex items-center justify-center gap-1.5">
                      <i class="fa-solid fa-flask text-[11px]"></i>
                      <span>Тест связки</span>
                    </button>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>
  </main>

  <!-- MODAL: Подключение нового аккаунта -->
  <div x-show="openAddModal" x-cloak class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm" x-transition>
    <div @click.outside="openAddModal = false" class="bg-slate-900 border border-slate-700 max-w-lg w-full rounded-2xl shadow-2xl p-6 space-y-5">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <h3 class="font-bold text-lg text-white flex items-center gap-2">
          <i class="fa-solid fa-plus-circle text-indigo-400"></i> Подключение amoCRM
        </h3>
        <button @click="openAddModal = false" class="text-slate-400 hover:text-white text-lg">&times;</button>
      </div>

      <div class="space-y-4">
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Поддомен amoCRM</label>
          <div class="flex items-center rounded-xl bg-slate-800 border border-slate-700 overflow-hidden focus-within:border-indigo-500">
            <input type="text" x-model="form.subdomain" placeholder="mycompany" class="bg-transparent px-3 py-2 text-sm w-full outline-none text-white">
            <span class="text-xs text-slate-400 pr-3 select-none">.amocrm.ru</span>
          </div>
          <p class="text-[11px] text-slate-400 mt-1">Только имя поддомена без https://</p>
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Название компании (опционально)</label>
          <input type="text" x-model="form.name" placeholder="Например: Мебельный салон" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500 text-white">
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Долгосрочный токен amoCRM</label>
          <textarea x-model="form.token" rows="4" placeholder="eyJ0eXAiOiJKV1QiLC..." class="w-full bg-slate-800 border border-slate-700 rounded-xl p-3 text-xs font-mono outline-none focus:border-indigo-500 text-slate-200"></textarea>
          <p class="text-[11px] text-slate-400 mt-1">Создаётся в amoCRM: Настройки → Интеграции → Создать интеграцию → Долгосрочный токен</p>
        </div>
      </div>

      <div class="flex items-center justify-end space-x-3 pt-2">
        <button @click="openAddModal = false" class="px-4 py-2 rounded-xl text-xs font-medium text-slate-400 hover:text-white">Отмена</button>
        <button @click="submitAddAccount()" :disabled="submitting" class="px-5 py-2 rounded-xl text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white flex items-center gap-2 shadow-lg shadow-indigo-600/30 disabled:opacity-50">
          <i class="fa-solid fa-spinner animate-spin" x-show="submitting"></i>
          <span x-text="submitting ? 'Подключение...' : 'Подключить'"></span>
        </button>
      </div>
    </div>
  </div>

  <!-- MODAL: Инструкции и настройка Salesbot после создания -->
  <div x-show="openWizardModal" x-cloak class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm" x-transition>
    <div @click.outside="openWizardModal = false" class="bg-slate-900 border border-slate-700 max-w-2xl w-full rounded-2xl shadow-2xl p-6 space-y-5">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <h3 class="font-bold text-lg text-white flex items-center gap-2">
          <i class="fa-solid fa-wand-magic-sparkles text-indigo-400"></i> Шаг 2: Настройка Salesbot в amoCRM
        </h3>
        <button @click="openWizardModal = false" class="text-slate-400 hover:text-white text-lg">&times;</button>
      </div>

      <div class="space-y-4 text-sm">
        <div class="p-3 bg-indigo-950/40 border border-indigo-800/40 rounded-xl space-y-1">
          <div class="text-xs font-semibold text-indigo-300">✨ Поле ответа создано автоматически:</div>
          <div class="text-xs text-slate-300">Поле <code class="text-indigo-300">"AI: Ответ ассистента"</code> получило ID <b x-text="wizardData.ai_reply_field_id"></b></div>
        </div>

        <div class="space-y-2">
          <div class="text-xs font-semibold text-slate-200">1. Текст для узла Salesbot:</div>
          <div class="flex items-center space-x-2">
            <input type="text" readonly :value="wizardData.salesbot_tag" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs font-mono text-indigo-300 outline-none">
            <button @click="copyText(wizardData.salesbot_tag)" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl text-xs flex items-center gap-1">
              <i class="fa-solid fa-copy"></i> Копировать
            </button>
          </div>
          <p class="text-[11px] text-slate-400">Вставьте эту строку в блок «Отправить сообщение» в конструкторе Salesbot.</p>
        </div>

        <div class="space-y-2">
          <div class="text-xs font-semibold text-slate-200">2. URL вебхука:</div>
          <div class="flex items-center space-x-2">
            <input type="text" readonly :value="wizardData.webhook_url" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs font-mono text-slate-300 outline-none">
            <button @click="copyText(wizardData.webhook_url)" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-xl text-xs flex items-center gap-1">
              <i class="fa-solid fa-copy"></i> Копировать
            </button>
          </div>
          <p class="text-[11px]" :class="wizardData.webhook_auto_registered ? 'text-emerald-400' : 'text-amber-400'">
            <i class="fa-solid" :class="wizardData.webhook_auto_registered ? 'fa-circle-check' : 'fa-circle-exclamation'"></i>
            <span x-text="wizardData.webhook_auto_registered ? 'Вебхук успешно зарегистрирован автоматически в amoCRM!' : 'Добавьте этот URL в Настройки → Интеграции → Webhooks (событие add_message)'"></span>
          </p>
        </div>

        <div class="p-3 bg-amber-950/30 border border-amber-800/40 rounded-xl text-xs text-amber-200 space-y-1">
          <div class="font-bold"><i class="fa-solid fa-triangle-exclamation"></i> Важные правила:</div>
          <div>1. Конструктор бота: строго 2 блока: <code>[Старт] ➔ [Отправить сообщение: {{wizardData.salesbot_tag}}]</code>.</div>
          <div>2. НЕ добавляйте блок «Вебхук» внутрь Salesbot.</div>
          <div>3. НЕ ставьте триггер автозапуска бота в Воронке на входящее сообщение. Бот запускается бэкендом!</div>
        </div>
      </div>

      <div class="flex items-center justify-end space-x-3 pt-2">
        <button @click="openWizardModal = false; openManageModal(selectedAccount)" class="px-5 py-2 rounded-xl text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/30">
          Выбрать Salesbot из списка ➔
        </button>
      </div>
    </div>
  </div>

  <!-- MODAL: Детальная настройка аккаунта (Боты, Воронки, Поля Экстрактора, Промпт ИИ) -->
  <div x-show="openSettingsModal" x-cloak class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm" x-transition>
    <div @click.outside="openSettingsModal = false" class="bg-slate-900 border border-slate-700 max-w-3xl w-full rounded-2xl shadow-2xl p-6 space-y-5 max-h-[90vh] flex flex-col">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div>
          <h3 class="font-bold text-lg text-white flex items-center gap-2">
            <i class="fa-solid fa-sliders text-indigo-400"></i>
            <span x-text="selectedAccount ? (selectedAccount.name || selectedAccount.subdomain) : ''"></span>
          </h3>
          <p class="text-xs text-slate-400">Настройка связки Salesbot, фильтра воронок и Экстрактора CRM</p>
        </div>
        <div class="flex items-center gap-2">
          <!-- Кнопка сохранения полей Экстрактора рядом с кнопкой синхронизации -->
          <button x-show="activeTab === 'fields'" @click="saveFields()" :disabled="savingFields" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white shadow-lg shadow-emerald-600/30 text-xs font-semibold transition flex items-center gap-1.5 disabled:opacity-50" title="Сохранить настройки полей Экстрактора">
            <i class="fa-solid" :class="savingFields ? 'fa-spinner animate-spin' : 'fa-floppy-disk text-[11px]'"></i>
            <span x-text="savingFields ? 'Сохранение...' : 'Сохранить поля'"></span>
          </button>

          <!-- Кнопка сохранения воронок (если открыта вкладка воронок) -->
          <button x-show="activeTab === 'pipelines'" @click="savePipelines()" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white shadow-lg shadow-emerald-600/30 text-xs font-semibold transition flex items-center gap-1.5" title="Сохранить выбранные воронки">
            <i class="fa-solid fa-floppy-disk text-[11px]"></i>
            <span>Сохранить воронки</span>
          </button>

          <!-- Кнопка сохранения настроек ИИ (если открыта вкладка ИИ) -->
          <button x-show="activeTab === 'ai'" @click="saveAIConfig()" class="px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white shadow-lg shadow-emerald-600/30 text-xs font-semibold transition flex items-center gap-1.5" title="Сохранить настройки ИИ">
            <i class="fa-solid fa-floppy-disk text-[11px]"></i>
            <span>Сохранить настройки ИИ</span>
          </button>

          <!-- Кнопка синхронизации с amoCRM -->
          <button @click="syncAccount()" :disabled="syncing" class="px-3 py-1.5 rounded-lg bg-indigo-600/30 hover:bg-indigo-600 border border-indigo-500/40 text-xs font-medium text-indigo-200 hover:text-white transition flex items-center gap-1.5 disabled:opacity-50" title="Запросить свежие воронки, поля и проверить бота в amoCRM">
            <i class="fa-solid fa-arrows-rotate text-[11px]" :class="syncing ? 'animate-spin' : ''"></i>
            <span x-text="syncing ? 'Синхронизация...' : 'Синхронизировать с amoCRM'"></span>
          </button>
          <button @click="openSettingsModal = false" class="text-slate-400 hover:text-white text-lg ml-2">&times;</button>
        </div>
      </div>

      <!-- Alert Banner if Account in Error State -->
      <div x-show="selectedAccount && (selectedAccount.status === 'error' || selectedAccount.last_error)" class="p-3 bg-rose-950/50 border border-rose-800/60 rounded-xl flex items-start gap-2.5 text-xs text-rose-200">
        <i class="fa-solid fa-triangle-exclamation text-rose-400 text-sm mt-0.5"></i>
        <div class="flex-1 space-y-1">
          <div class="font-semibold text-rose-300">Ошибка подключения к amoCRM:</div>
          <div x-text="selectedAccount.last_error"></div>
          <div class="text-[11px] text-rose-400/90 pt-1">
            Чтобы обновить токен: используйте кнопку «Подключить amoCRM» на главной странице и введите новый токен (или оплатите подписку amoCRM и нажмите кнопку запуска).
          </div>
        </div>
      </div>

      <!-- Tabs Navigation -->
      <div class="flex space-x-2 border-b border-slate-800 pb-2 text-xs font-medium">
        <button @click="activeTab = 'bot'" :class="activeTab === 'bot' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white bg-slate-800'" class="px-3 py-1.5 rounded-lg transition">
          <i class="fa-solid fa-robot mr-1"></i> 1. Привязка Salesbot
        </button>
        <button @click="activeTab = 'pipelines'" :class="activeTab === 'pipelines' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white bg-slate-800'" class="px-3 py-1.5 rounded-lg transition">
          <i class="fa-solid fa-filter mr-1"></i> 2. Воронки
        </button>
        <button @click="activeTab = 'fields'" :class="activeTab === 'fields' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white bg-slate-800'" class="px-3 py-1.5 rounded-lg transition">
          <i class="fa-solid fa-table-columns mr-1"></i> 3. Экстрактор полей
        </button>
        <button @click="activeTab = 'ai'" :class="activeTab === 'ai' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white bg-slate-800'" class="px-3 py-1.5 rounded-lg transition">
          <i class="fa-solid fa-brain mr-1"></i> 4. Промпт и ИИ
        </button>
      </div>

      <!-- Tab 1: Salesbot -->
      <div x-show="activeTab === 'bot'" class="space-y-4 flex-1 overflow-y-auto pr-1">
        <div class="space-y-2">
          <label class="block text-xs font-medium text-slate-300">Выберите собранный Salesbot из amoCRM:</label>
          <div class="flex items-center space-x-2">
            <select x-model="selectedBotId" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white outline-none focus:border-indigo-500">
              <option value="">-- Выберите бота --</option>
              <template x-for="bot in availableBots" :key="bot.id">
                <option :value="bot.id" x-text="bot.name + ' (ID: ' + bot.id + ')'"></option>
              </template>
            </select>
            <button @click="loadBots()" class="px-3 py-2 bg-slate-800 hover:bg-slate-700 rounded-xl text-xs text-slate-300">
              <i class="fa-solid fa-rotate"></i>
            </button>
          </div>
        </div>

        <div class="flex items-center space-x-3 pt-2">
          <button @click="saveBotLink()" :disabled="!selectedBotId" class="px-4 py-2 rounded-xl text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white disabled:opacity-50">
            Сохранить привязку бота
          </button>
          <button @click="testBot(selectedAccount.id)" class="px-4 py-2 rounded-xl text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white">
            <i class="fa-solid fa-paper-plane mr-1"></i> Проверить связку (Тест)
          </button>
        </div>
      </div>

      <!-- Tab 2: Pipelines -->
      <div x-show="activeTab === 'pipelines'" class="space-y-4 flex-1 overflow-y-auto pr-1">
        <p class="text-xs text-slate-400">Отметьте воронки, в которых ИИ-менеджер должен автоматически отвечать клиентам:</p>
        <div class="space-y-2">
          <template x-for="p in pipelines" :key="p.amo_pipeline_id">
            <label class="flex items-center space-x-3 p-3 bg-slate-800/60 rounded-xl border border-slate-700 hover:border-slate-600 cursor-pointer" :class="p.is_deleted_in_amo ? 'opacity-60 border-rose-900/40' : ''">
              <input type="checkbox" :checked="p.is_enabled" :disabled="p.is_deleted_in_amo" @change="p.is_enabled = $event.target.checked" class="rounded text-indigo-600 focus:ring-indigo-500 w-4 h-4 bg-slate-700 disabled:opacity-40">
              <div class="text-sm font-medium" :class="p.is_deleted_in_amo ? 'text-slate-400 line-through' : 'text-white'" x-text="p.name"></div>
              <span x-show="p.is_deleted_in_amo" class="text-[10px] font-semibold px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 border border-rose-700/50">
                <i class="fa-solid fa-triangle-exclamation mr-0.5"></i> Удалена в amoCRM
              </span>
              <div class="text-xs text-slate-500 ml-auto" x-text="'ID: ' + p.amo_pipeline_id"></div>
            </label>
          </template>
        </div>
      </div>

      <!-- Tab 3: Fields Extractor -->
      <div x-show="activeTab === 'fields'" class="space-y-4 flex-1 overflow-y-auto pr-1">
        <p class="text-xs text-slate-400">Выберите поля сделки или контакта, которые ИИ-экстрактор будет извлекать из диалога и автоматически сохранять в CRM:</p>
        <div class="space-y-3">
          <template x-for="f in fields" :key="f.amo_field_id">
            <div class="p-3 bg-slate-800/60 rounded-xl border border-slate-700 space-y-2" :class="f.is_deleted_in_amo ? 'opacity-60 border-rose-900/40' : ''">
              <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                  <label class="flex items-center space-x-2 cursor-pointer">
                    <input type="checkbox" :checked="f.is_enabled" :disabled="f.is_deleted_in_amo" @change="f.is_enabled = $event.target.checked" class="rounded text-indigo-600 w-4 h-4 bg-slate-700 disabled:opacity-40">
                    <span class="text-sm font-medium" :class="f.is_deleted_in_amo ? 'text-slate-400 line-through' : 'text-white'" x-text="f.field_name"></span>
                  </label>
                  <!-- Бейдж типа сущности -->
                  <span x-show="!f.is_deleted_in_amo && f.entity_type === 'contact'"
                        class="text-[10px] font-semibold px-2 py-0.5 rounded bg-sky-900/60 text-sky-300 border border-sky-700/50">
                    <i class="fa-solid fa-user mr-0.5"></i> Контакт
                  </span>
                  <span x-show="!f.is_deleted_in_amo && (f.entity_type === 'lead' || !f.entity_type)"
                        class="text-[10px] font-semibold px-2 py-0.5 rounded bg-violet-900/60 text-violet-300 border border-violet-700/50">
                    <i class="fa-solid fa-handshake mr-0.5"></i> Сделка
                  </span>
                  <span x-show="f.is_deleted_in_amo" class="text-[10px] font-semibold px-2 py-0.5 rounded bg-rose-900/60 text-rose-300 border border-rose-700/50">
                    <i class="fa-solid fa-triangle-exclamation mr-0.5"></i> Удалено в amoCRM
                  </span>
                  <button x-show="f.is_deleted_in_amo" @click="deleteFieldMapping(f)" class="text-[10px] text-rose-400 hover:text-white px-2 py-0.5 rounded bg-rose-950/80 border border-rose-800/80 hover:bg-rose-800 transition flex items-center gap-1" title="Удалить это поле из панели">
                    <i class="fa-solid fa-trash-can text-[9px]"></i> Удалить
                  </button>
                </div>
                <span class="text-xs text-slate-500" x-text="f.field_type + ' (ID: ' + f.amo_field_id + ')'"></span>
              </div>
              <div x-show="f.is_enabled && !f.is_deleted_in_amo" class="grid grid-cols-1 md:grid-cols-2 gap-2 pt-1">
                <div>
                  <label class="text-[10px] text-slate-400">Подсказка для ИИ (что искать):</label>
                  <input type="text" x-model="f.ai_hint" class="w-full bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-xs text-white">
                </div>
                <div class="flex items-center space-x-2 pt-4">
                  <input type="checkbox" :checked="f.overwrite_if_filled" @change="f.overwrite_if_filled = $event.target.checked" class="rounded text-indigo-600 w-3.5 h-3.5 bg-slate-700">
                  <label class="text-xs text-slate-300">Перезаписывать, если уже заполнено</label>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!-- Tab 4: AI Config -->
      <div x-show="activeTab === 'ai'" class="space-y-4 flex-1 overflow-y-auto pr-1">
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">Системный промпт Общителя</label>
          <textarea x-model="aiConfig.communicator_prompt" rows="4" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-3 text-xs text-slate-200 outline-none focus:border-indigo-500"></textarea>
        </div>

        <!-- Секция: База знаний и каталог продуктов -->
        <div class="p-3.5 bg-slate-800/80 border border-slate-700 rounded-xl space-y-3">
          <div class="flex items-center justify-between">
            <label class="text-xs font-semibold text-indigo-300 flex items-center gap-1.5">
              <i class="fa-solid fa-book-bookmark text-indigo-400"></i> Каталог продуктов и База знаний
            </label>
            <span class="text-[11px] text-slate-400" x-show="aiConfig && aiConfig.knowledge_base">
              Символов: <span class="font-mono text-indigo-300" x-text="(aiConfig && aiConfig.knowledge_base) ? aiConfig.knowledge_base.length : 0"></span>
            </span>
          </div>

          <div class="space-y-1">
            <label class="block text-[11px] font-medium text-slate-300">Режим работы с каталогом продуктов:</label>
            <select x-model="aiConfig.knowledge_mode" class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white outline-none focus:border-indigo-500">
              <option value="plain_text">Простой текст в промпте (In-Context) — до 50 товаров/услуг</option>
              <option value="gemini_cache">Google Gemini Context Caching (Большой каталог >32k токенов)</option>
              <option value="disabled">Отключено (без каталога)</option>
            </select>
          </div>

          <div x-show="aiConfig.knowledge_mode !== 'disabled'" class="space-y-2">
            <label class="block text-[11px] font-medium text-slate-300">Описание товаров, услуг, цен и условий:</label>
            <textarea x-model="aiConfig.knowledge_base" rows="5"
                      placeholder="### Товар 1: Название&#10;- Цена: 25 000 руб.&#10;- Для кого: Малый бизнес&#10;- Что входит: Описание продукта...&#10;- Триггер для рекомендации: Если клиент спрашивает про автоматизацию...&#10;&#10;### Товар 2: Название...&#10;- Цена: 50 000 руб."
                      class="w-full bg-slate-900 border border-slate-700 rounded-xl p-3 text-xs font-mono text-slate-200 outline-none focus:border-indigo-500"></textarea>

            <div class="p-2.5 bg-indigo-950/40 border border-indigo-800/40 rounded-lg text-[11px] text-indigo-200 space-y-1">
              <div class="font-medium text-indigo-300 flex items-center gap-1">
                <i class="fa-solid fa-circle-info"></i> Как ИИ использует эту базу:
              </div>
              <div>• На обычные приветствия («Привет», «Здравствуйте») каталог <strong>НЕ вываливается</strong>.</div>
              <div>• ИИ рекомендует конкретный продукт только тогда, когда клиент сам спросил о ценах/товарах или когда в ходе диалога стали понятны его потребности.</div>
              <div x-show="aiConfig.knowledge_mode === 'gemini_cache'" class="text-amber-300 pt-1">
                <i class="fa-solid fa-bolt text-amber-400"></i> Режим Gemini Cache: для кэширования в Google TPU требуется от ~32k токенов. При меньшем объёме система автоматически передаст текст напрямую в промпт без ошибок.
              </div>
            </div>
          </div>
        </div>

        <!-- Секция: Публичные комментарии в соцсетях и Direct -->
        <div class="p-3.5 bg-slate-800/80 border border-slate-700 rounded-xl space-y-3">
          <div class="flex items-center justify-between">
            <label class="text-xs font-semibold text-sky-300 flex items-center gap-1.5">
              <i class="fa-solid fa-comments text-sky-400"></i> Автоответы на комментарии и перевод в Direct
            </label>
          </div>

          <div>
            <label class="block text-[11px] font-medium text-slate-300 mb-1">Ссылка на Direct компании (Instagram / Telegram / др.):</label>
            <input type="text" x-model="aiConfig.direct_link" placeholder="https://ig.me/m/marketingmarkaziuz"
                   class="w-full bg-slate-900 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white outline-none focus:border-indigo-500 font-mono">
            <div class="text-[10px] text-slate-400 mt-0.5">ИИ будет прикреплять эту ссылку в ответ на комментарии, чтобы клиент в 1 клик переходил в личные сообщения.</div>
          </div>

          <div class="space-y-1">
            <label class="block text-[11px] font-medium text-slate-300">Промпт для комментариев под постами (на узбекском / русском):</label>
            <textarea x-model="aiConfig.comment_prompt" rows="7"
                      class="w-full bg-slate-900 border border-slate-700 rounded-xl p-3 text-xs text-slate-200 outline-none focus:border-indigo-500 font-mono"></textarea>
            <div class="text-[10px] text-slate-400">Когда сообщение приходит из комментария под постом, ИИ использует эту готовую инструкцию. Вы можете свободно редактировать правила и фразы под себя.</div>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">Модель Общителя (Основная)</label>
            <input type="text" x-model="aiConfig.communicator_model" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
          </div>
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">Модель Экстрактора (Основная)</label>
            <input type="text" x-model="aiConfig.extractor_model" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1 flex items-center gap-1">
              <i class="fa-solid fa-shield-halved text-emerald-400 text-[11px]"></i>
              <span>Запасная модель Общителя</span>
            </label>
            <input type="text" x-model="aiConfig.fallback_communicator_model" placeholder="gemini-2.5-flash" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
            <div class="text-[10px] text-slate-400 mt-0.5">Включается при сбое/перегрузке (503, 429, таймаут)</div>
          </div>
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1 flex items-center gap-1">
              <i class="fa-solid fa-shield-halved text-emerald-400 text-[11px]"></i>
              <span>Запасная модель Экстрактора</span>
            </label>
            <input type="text" x-model="aiConfig.fallback_extractor_model" placeholder="gemini-2.5-flash" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
            <div class="text-[10px] text-slate-400 mt-0.5">Включается при сбое/перегрузке для извлечения полей</div>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">Температура генерации (0.0 - 1.0)</label>
            <input type="number" step="0.1" min="0" max="1" x-model="aiConfig.temperature" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
          </div>
          <div>
            <label class="block text-xs font-medium text-slate-300 mb-1">Лимит застревания (Handover)</label>
            <input type="number" min="1" max="10" x-model="aiConfig.handover_after_stuck" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-xs text-white">
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const DEFAULT_COMMENT_PROMPT_TEXT = `Ты — вежливый и дружелюбный ИИ-менеджер. Твоя задача — отвечать на комментарии клиентов под постами и Reels в соцсетях.

ПРАВИЛА ОТВЕТА:
1. Отвечай строго на том языке, на котором написал клиент (узбекский или русский).
2. Если клиент прислал '+', '++', огонёк '🔥', смайлик или вопрос о цене/наличии:
   - На узбекском: «Assalomu alaykum! Qiziqishingiz uchun rahmat. Narxlar va batafsil ma'lumotni Direct-ga yubordik 👉 {direct_link} (yoki shaxsiy xabarlaringizni tekshiring 📩)»
   - На русском: «Здравствуйте! Спасибо за интерес! Отправили подробности и цены вам в Direct 👉 {direct_link} (или проверьте личные сообщения 📩)»
3. Если клиент задал конкретный вопрос по товару или услуге — ответь на вопрос кратко (1-2 предложения) по базе знаний и обязательно предложи продолжить в Direct: {direct_link}.
4. Твой ответ публичный, поэтому держи его кратким, дружелюбным и без длинных списков вопросов.`;

    function adminApp() {
      return {
        adminKey: localStorage.getItem('ai_admin_key') || '__DEFAULT_ADMIN_KEY__',
        adminKeyInput: '',
        showAuthModal: false,
        accounts: [],
        loading: false,
        syncing: false,
        savingFields: false,
        openAddModal: false,
        openWizardModal: false,
        openSettingsModal: false,
        submitting: false,
        activeTab: 'bot',
        selectedAccount: null,
        selectedBotId: '',
        availableBots: [],
        pipelines: [],
        aiConfig: {
          communicator_prompt: '',
          communicator_model: 'gemini-3.1-flash-lite',
          fallback_communicator_model: 'gemini-2.5-flash',
          extractor_model: 'gemini-3.1-flash-lite',
          fallback_extractor_model: 'gemini-2.5-flash',
          temperature: 0.4,
          handover_after_stuck: 4,
          knowledge_base: '',
          knowledge_mode: 'plain_text',
          comment_prompt: DEFAULT_COMMENT_PROMPT_TEXT,
          direct_link: ''
        },
        form: {
          subdomain: '',
          name: '',
          token: ''
        },
        wizardData: {
          ai_reply_field_id: '',
          salesbot_tag: '',
          webhook_url: '',
          webhook_auto_registered: false
        },
        toast: {
          show: false,
          message: '',
          type: 'success'
        },
        showToast(msg, type = 'success') {
          this.toast.message = msg;
          this.toast.type = type;
          this.toast.show = true;
          setTimeout(() => { this.toast.show = false; }, 4000);
        },
        copyText(text) {
          navigator.clipboard.writeText(text);
          this.showToast('Скопировано в буфер обмена!');
        },
        formatStatus(st, lastError) {
          if (st === 'error') {
            return lastError || 'Ошибка amoCRM';
          }
          const map = {
            'pending_validation': 'Проверка токена',
            'field_created': 'Поле создано',
            'awaiting_manual_bot': 'Ожидает Salesbot',
            'bot_linked': 'Salesbot привязан',
            'awaiting_pipeline_setup': 'Настройка воронок',
            'configured': 'Сконфигурирован',
            'verified': 'Верифицирован (Работает)',
            'error': 'Ошибка'
          };
          return map[st] || st;
        },
        async apiFetch(url, options = {}) {
          const headers = { ...(options.headers || {}) };
          if (this.adminKey) {
            headers['X-Admin-Key'] = this.adminKey;
          }
          const res = await fetch(url, { ...options, headers });
          if (res.status === 401 || res.status === 403) {
            this.showAuthModal = true;
          }
          return res;
        },
        async submitAdminKey() {
          this.adminKey = this.adminKeyInput.trim();
          localStorage.setItem('ai_admin_key', this.adminKey);
          document.cookie = `admin_key=${encodeURIComponent(this.adminKey)}; path=/; max-age=2592000; SameSite=Lax`;
          this.showAuthModal = false;
          await this.fetchAccounts();
        },
        logoutAdmin() {
          this.adminKeyInput = this.adminKey;
          this.showAuthModal = true;
        },
        async init() {
          if (!this.adminKey) {
            this.showAuthModal = true;
          }
          await this.fetchAccounts();
        },
        async fetchAccounts() {
          this.loading = true;
          try {
            const res = await this.apiFetch('/accounts');
            if (res.ok) {
              const data = await res.json();
              this.accounts = Array.isArray(data) ? data : [];
            } else {
              this.accounts = [];
              this.showToast('Ошибка авторизации API (проверьте ADMIN_API_KEY)', 'error');
            }
          } catch (e) {
            this.accounts = [];
            this.showToast('Ошибка загрузки аккаунтов', 'error');
          } finally {
            this.loading = false;
          }
        },
        async toggleAccountActive(acc) {
          try {
            const res = await this.apiFetch(`/accounts/${acc.id}/toggle-active`, {
              method: 'POST'
            });
            const data = await res.json();
            if (res.ok) {
              acc.is_active = data.is_active;
              acc.status = data.status;
              acc.last_error = data.last_error;
              this.showToast(data.message, acc.is_active ? 'success' : 'error');
            } else {
              await this.fetchAccounts();
              throw new Error(data.detail || 'Ошибка переключения');
            }
          } catch (e) {
            this.showToast(e.message, 'error');
          }
        },
        async submitAddAccount() {
          if (!this.form.subdomain || !this.form.token) {
            this.showToast('Укажите поддомен и токен', 'error');
            return;
          }
          this.submitting = true;
          try {
            const res = await this.apiFetch('/accounts', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(this.form)
            });
            const data = await res.json();
            if (!res.ok) {
              throw new Error(data.detail || 'Ошибка подключения');
            }
            this.showToast('Аккаунт успешно подключен!');
            this.openAddModal = false;
            this.wizardData = data;
            this.openWizardModal = true;
            await this.fetchAccounts();
            this.selectedAccount = this.accounts.find(a => a.id === data.account_id);
          } catch (e) {
            this.showToast(e.message, 'error');
          } finally {
            this.submitting = false;
          }
        },
        async openManageModal(acc) {
          this.selectedAccount = acc;
          this.selectedBotId = acc.bot_id || '';
          this.openSettingsModal = true;
          this.activeTab = 'bot';
          await Promise.all([
            this.loadBots(),
            this.loadPipelines(),
            this.loadFields(),
            this.loadAIConfig()
          ]);
        },
        async syncAccount() {
          if (!this.selectedAccount) return;
          this.syncing = true;
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/sync`, {
              method: 'POST'
            });
            const data = await res.json();
            if (res.ok) {
              this.showToast(data.message || 'Данные синхронизированы с amoCRM!');
              await Promise.all([
                this.loadBots(),
                this.loadPipelines(),
                this.loadFields(),
                this.loadAIConfig(),
                this.fetchAccounts()
              ]);
              const updated = this.accounts.find(a => a.id === this.selectedAccount.id);
              if (updated) this.selectedAccount = updated;
            } else {
              throw new Error(data.detail || 'Ошибка синхронизации');
            }
          } catch (e) {
            this.showToast(e.message, 'error');
          } finally {
            this.syncing = false;
          }
        },
        async loadBots() {
          if (!this.selectedAccount) return;
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/bots`);
            this.availableBots = await res.json();
          } catch (e) {
            this.showToast('Ошибка получения ботов', 'error');
          }
        },
        async saveBotLink() {
          try {
            const newBotId = parseInt(this.selectedBotId);
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/link-bot`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ bot_id: newBotId })
            });
            if (res.ok) {
              this.selectedAccount.bot_id = newBotId;
              this.showToast('Salesbot успешно привязан!');
              await this.fetchAccounts();
            } else {
              throw new Error();
            }
          } catch (e) {
            this.showToast('Не удалось привязать бота', 'error');
          }
        },
        async testBot(accId) {
          this.showToast('Отправка проверочного запроса в amoCRM...');
          try {
            const res = await this.apiFetch(`/accounts/${accId}/test-connection`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({})
            });
            const data = await res.json();
            if (res.ok) {
              this.showToast(data.message || 'Связка работает (202 Accepted)!');
            } else {
              throw new Error(data.detail || 'Ошибка теста связки');
            }
          } catch (e) {
            this.showToast(e.message, 'error');
          } finally {
            await this.fetchAccounts();
          }
        },
        async loadPipelines() {
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/pipelines`);
            this.pipelines = await res.json();
          } catch (e) {}
        },
        async savePipelines() {
          try {
            const enabledIds = this.pipelines.filter(p => p.is_enabled).map(p => p.amo_pipeline_id);
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/pipelines`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ enabled_amo_pipeline_ids: enabledIds })
            });
            if (res.ok) {
              this.showToast('Воронки успешно сохранены!');
            }
          } catch (e) {
            this.showToast('Ошибка сохранения воронок', 'error');
          }
        },
        async loadFields() {
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/fields`);
            this.fields = await res.json();
          } catch (e) {}
        },
        async saveFields() {
          this.savingFields = true;
          try {
            const payload = {
              fields: this.fields.map(f => ({
                amo_field_id: f.amo_field_id,
                is_enabled: f.is_enabled,
                ai_hint: f.ai_hint,
                overwrite_if_filled: f.overwrite_if_filled
              }))
            };
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/fields`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload)
            });
            if (res.ok) {
              this.showToast('Настройки полей сохранены!');
            }
          } catch (e) {
            this.showToast('Ошибка сохранения полей', 'error');
          } finally {
            this.savingFields = false;
          }
        },
        async deleteFieldMapping(f) {
          if (!confirm(`Удалить поле «${f.field_name}» из списка панели?`)) return;
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/fields/${f.amo_field_id}`, {
              method: 'DELETE'
            });
            if (res.ok) {
              this.fields = this.fields.filter(item => item.amo_field_id !== f.amo_field_id);
              this.showToast(`Поле «${f.field_name}» удалено из списка`);
            } else {
              throw new Error();
            }
          } catch (e) {
            this.showToast('Ошибка удаления поля', 'error');
          }
        },
        async loadAIConfig() {
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/ai-config`);
            const data = await res.json();
            if (!data.knowledge_mode) data.knowledge_mode = 'plain_text';
            if (!data.knowledge_base) data.knowledge_base = '';
            if (!data.fallback_communicator_model) data.fallback_communicator_model = 'gemini-2.5-flash';
            if (!data.fallback_extractor_model) data.fallback_extractor_model = 'gemini-2.5-flash';
            if (!data.comment_prompt) data.comment_prompt = DEFAULT_COMMENT_PROMPT_TEXT;
            if (!data.direct_link) data.direct_link = '';
            this.aiConfig = data;
          } catch (e) {
            console.error('Ошибка загрузки настроек ИИ:', e);
          }
        },
        async saveAIConfig() {
          try {
            const res = await this.apiFetch(`/accounts/${this.selectedAccount.id}/ai-config`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(this.aiConfig)
            });
            if (res.ok) {
              this.showToast('Настройки ИИ сохранены!');
            }
          } catch (e) {
            this.showToast('Ошибка сохранения настроек ИИ', 'error');
          }
        }
      }
    }
  </script>
</body>
</html>
"""

@router.get("/admin", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def serve_admin_panel():
    """Главная страница панели управления amoCRM AI Manager"""
    html = ADMIN_HTML.replace("__DEFAULT_ADMIN_KEY__", settings.ADMIN_API_KEY or "")
    response = HTMLResponse(content=html)
    if settings.ADMIN_API_KEY:
        response.set_cookie(
            key="admin_key",
            value=settings.ADMIN_API_KEY,
            max_age=2592000,
            path="/",
            samesite="lax",
        )
    return response
