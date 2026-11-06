CRIADOR DE ANUNCIOS SHOPEE  (servico separado do painel agente-shopee)

O QUE FAZ
1. Analisar: compara os anuncios ativos do Mercado Livre (Duplo Giro) com os SKUs da Shopee.
2. Busca custo, peso e medidas de cada SKU que falta no Bling da irma e calcula o preco.
3. Voce marca os itens e envia de 50 em 50. Fotos e descricao vem do Mercado Livre.
Uma vez por semana: abrir o app e clicar em "Analisar".

REGRA DE PRECO (configuravel na tela)
preco anunciado - 8% da promocao - taxa Shopee (sobre o preco pago) - imposto - custo = 20% do preco anunciado
Taxas: ate 79,99 = 20% + R$4 | 80 a 99,99 = 14% + R$16 | 100 a 199,99 = 14% + R$20 | 200+ = 14% + R$26
Piso: nunca abaixo de R$29,99. O preco final termina em ,99. Imposto padrao: 9,45%.

COMO COLOCAR NO AR (Render, plano gratuito, repositorio novo no GitHub: criador-shopee)
1. GitHub: novo repositorio privado "criador-shopee" com app.py, requirements.txt e README.txt.
2. Neon: pode usar o mesmo projeto (agente-shopee); as tabelas deste app comecam com "cria_" e nao
   mexem nas do painel. Copie a connection string (DATABASE_URL).
3. Render: New > Web Service > repositorio criador-shopee.
   Build: pip install -r requirements.txt
   Start: gunicorn app:app --workers 1 --threads 4 --timeout 120
4. Variaveis de ambiente no Render:
   APP_BASE_URL        https://NOME-DO-SERVICO.onrender.com   (sem barra no final)
   ADMIN_USER / ADMIN_PASS   login do app (obrigatorio)
   DATABASE_URL        connection string do Neon
   DRY_RUN             1  (simulacao; so mude para 0 quando o teste estiver certo)
   SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY    (Live Partner_id do app "Painel Duplo Giro")
   ML_CLIENT_ID / ML_CLIENT_SECRET           (app "duplo" do Mercado Livre)
   BLING_CLIENT_ID / BLING_CLIENT_SECRET     (app criado no Bling DA IRMA)
5. Cadastrar as URLs de retorno (redirect) de cada plataforma:
   Shopee: https://NOME-DO-SERVICO.onrender.com/callback/shopee
   Mercado Livre: https://NOME-DO-SERVICO.onrender.com/callback/ml
   Bling da irma: https://NOME-DO-SERVICO.onrender.com/callback/bling
6. Abrir o app, clicar em Conectar nas tres linhas e testar em simulacao.

ATENCAO
- O Bling e privado por conta: sua irma precisa criar o aplicativo dentro do Bling DELA
  (Central de extensoes > aplicativos) e autorizar. O app "Agente Shopee" do seu Bling nao serve para a conta dela.
- Autorizar a Shopee de novo por este app pode derrubar a conexao do painel antigo (o refresh token
  da Shopee e de uso unico). Se isso acontecer, reconecte a Shopee no painel antigo.
- Itens do ML com variacoes e sem SKU no nivel do anuncio sao ignorados (o app informa quantos).
- O campo Bluetooth e outros atributos obrigatorios sao preenchidos pela tela de configuracoes
  (padrao: Bluetooth = Nao). Se faltar um valor, o item fica "Bloqueado" e mostra as opcoes.
- Primeiro teste: deixe DRY_RUN=1, analise, marque 1 item e envie. Ele fica "Simulado" com a categoria e
  os atributos que seriam usados. Depois mude DRY_RUN para 0, marque so 1 item e envie de verdade.
