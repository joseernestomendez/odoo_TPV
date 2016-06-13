# -*- coding: utf-8 -*-
import json
import os
import urllib2
try:
    from paramiko import SSHException
except ImportError:
    pass
from openerp.exceptions import ValidationError
from ..neoutil import neoutil

from openerp import models, fields, api
from openerp.osv import osv, fields as oldField
from pprint import pprint


class FiscalPrinter(models.Model):
    _name = 'neotec_interface.fiscal_printer'

    name = fields.Char(string="Nombre", required=True)
    invoice_directory = fields.Char(string="Directorio Facturas", required=True)
    copy_quantity = fields.Integer(string="Cantidad Copias", default=0)
    description = fields.Text(string=u"Descripción")
    bd = fields.Integer(string=u"División de Negocios", required=True)
    ep = fields.Integer(string="Sucursal", required=True)  # Punto de Emisión
    ia = fields.Integer(string="Caja", required=True)  # Area de Impresión
    charge_legal_tip = fields.Boolean(string="Cargar Propina Legal", default=False,
                                      help="Carga 10% de propina legal a la cuenta del cliente en el modulo de restaurantes")

    ftp_user = fields.Char(string="Usuario")
    ftp_pwd = fields.Char(string=u"Contraseña")
    ftp_ip = fields.Char(string=u"Dirección Terminar Impresión")

    ncf_range_ids = fields.One2many("neotec_interface.ncf_range", "fiscal_printer_id", "Secuencias de NCF")

    def create(self, cr, uid, vals, context=None):
        invoice_dir = vals.get("invoice_directory", False)

        if not os.path.exists(invoice_dir):
            os.makedirs(invoice_dir)

        return super(FiscalPrinter, self).create(cr, uid, vals, context=context)

    @api.model
    def register_invoice(self, invoice):

        if invoice:

            invoice['density'] = ''
            invoice['logo'] = ''
            invoice['ncf']['bd'] = str(invoice['ncf']['bd']).zfill(2)
            invoice['ncf']['office'] = str(invoice['ncf']['office']).zfill(3)
            invoice['ncf']['box'] = str(invoice['ncf']['box']).zfill(3)
            invoice['copyQty'] = str(invoice['copyQty'])
            invoice['tip'] = str(invoice['tip'])

            if invoice['client'] is None:
                invoice['client'] = {'name': '', 'rnc': ''}

            for item in invoice['items']:
                tax = self.env['account.tax'].browse(item['taxId'])
                # if item doesnt have tax, 18% is default
                tax_amount = 18.0
                if tax:
                    tax_amount = tax.amount
                if tax_amount == 0: # no tax applicable for this item
                    tax_amount = 1000
                item['tax'] = str(tax_amount).replace('.', '') + '0'
                item['price'] = str(item['price'])
                item['quantity'] = ("%0.3f" % float(item['quantity'])).replace('.', '')
                item['type'] = str(item['type'])

            for payment in invoice['payments']:
                payment_type = self.env['neotec_interface.payment_type'].search(
                    [['account_journal_id', '=', payment['id']]])

                payment['amount'] = '{:.2f}'.format(payment['amount']).replace('.', '')

                if payment_type.code == 0:
                    invoice['effectivePayment'] = payment['amount']
                elif payment_type.code == 1:
                    invoice['checkPayment'] = payment['amount']
                elif payment_type.code == 2:
                    invoice['creditCardPayment'] = payment['amount']
                elif payment_type.code == 3:
                    invoice['debitCardPayment'] = payment['amount']
                elif payment_type.code == 4:
                    invoice['ownCardPayment'] = payment['amount']
                elif payment_type.code == 5:
                    invoice['voucherPayment'] = payment['amount']
                elif payment_type.code == 6:
                    invoice['other1Payment'] = payment['amount']
                elif payment_type.code == 7:
                    invoice['other2Payment'] = payment['amount']
                elif payment_type.code == 8:
                    invoice['other3Payment'] = payment['amount']
                elif payment_type.code == 9:
                    invoice['other4Payment'] = payment['amount']
                elif payment_type.code == 10:
                    invoice['creditNotePayment'] = payment['amount']

            ncf_type = self.env['neotec_interface.ncf_type'].browse(invoice['ncf']['ncfTypeId'])

            if ncf_type.ttr == 1:  # Fiscal Credit
                invoice['type'] = '2'
            elif ncf_type.ttr == 15:  # Governmental
                invoice['type'] = '2'
            elif ncf_type.ttr == 14:  # Special Regime
                invoice['type'] = '6'
            elif ncf_type.ttr == 4:  # TODO In case of Credit Note the 'type' will be sent from the frontend
                pass
            else:  # Final Consumer ttr = 2
                invoice['type'] = '1'

            sequence = ''

            ncf_range = self.env['neotec_interface.ncf_range'].search(
                [('fiscal_printer_id', '=', invoice['fiscalPrinterId']),
                 ('ncf_type_id', '=', invoice['ncf']['ncfTypeId'])])

            if ncf_range.remaining_quantity <= 0:
                raise ValidationError("No le quedan NCFs \"" + ncf_type.name + "\", realize su solicitud")

            next_range = ncf_range.used_quantity + 1
            ncf_range.used_quantity = next_range
            sequence = str(next_range).zfill(8)

            ncf = ncf_type.serie + invoice['ncf']['bd'] + invoice['ncf']['office'] + invoice['ncf']['box'] + str(
                ncf_type.ttr).zfill(2) + sequence

            invoice['ncfString'] = ncf

            if 'orderReference' in invoice:
                current_order = self.env['pos.order'].search([('pos_reference', '=', invoice['orderReference'])], limit=1)
                fiscal_printer = self.env['neotec_interface.fiscal_printer'].browse(invoice['fiscalPrinterId'])
                current_order.ncf = ncf
                current_order.using_legal_tip = fiscal_printer.charge_legal_tip

                if invoice['deliveryAddress']:
                    current_order.delivery_address = invoice['deliveryAddress']
                    current_order.is_delivery_order = invoice['deliveryAddress']

            if invoice['referenceNcf'] != '':

                for item in invoice['items']:
                    order_line = self.env['pos.order.line'].browse(item['orderLineId'])
                    order_line.unlink()

            file_name = str(ncf)

            if not os.path.exists(invoice['directory']):
                os.mkdir(invoice['directory'])

            f = open(invoice['directory'] + '/' + file_name + '.txt', 'w')
            formatted_invoice = neoutil.format_invoice(invoice)
            f.write(formatted_invoice)
            f.close()

            fiscal_printer = self.env['neotec_interface.fiscal_printer'].browse(invoice['fiscalPrinterId'])

            ftp_conf = {'ftp_user': fiscal_printer.ftp_user, 'ftp_pwd': fiscal_printer.ftp_pwd,
                        'ftp_ip': fiscal_printer.ftp_ip}

            path_parts = fiscal_printer.invoice_directory.split('/')
            last_dir = path_parts[len(path_parts) - 1]
            remote_path_conf = {'path': last_dir, 'file_name': ncf}

            try:
                neoutil.send_invoice_to_terminal(formatted_invoice, ftp_conf, remote_path_conf)
            except SSHException:
                raise ValidationError("No se pudo conectar con la terminar de impresión")


class NCFRange(models.Model):
    _name = 'neotec_interface.ncf_range'

    total_quantity = fields.Integer(string="Cantidad", required=True)
    used_quantity = fields.Integer(string="Usados", required=True)
    remaining_quantity = fields.Integer(compute="_compute_remaining", string="Restantes")
    ncf_type_id = fields.Many2one("neotec_interface.ncf_type", string="Tipo NCF", required=True)
    fiscal_printer_id = fields.Many2one("neotec_interface.fiscal_printer", string="Impresora")

    @api.one
    @api.depends('used_quantity', 'total_quantity')
    def _compute_remaining(self):
        self.remaining_quantity = self.total_quantity - self.used_quantity


class NCF(models.Model):
    _name = 'neotec_interface.ncf'

    name = fields.Char(string="Sequencia")
    fiscal_printer_id = fields.Many2one("neotec_interface.fiscal_printer", string="Impresora")
    ncf_type_id = fields.Many2one("neotec_interface.ncf_type")


class NCFType(models.Model):
    _name = 'neotec_interface.ncf_type'
    name = fields.Char(string="Nombre")
    serie = fields.Char(string="Serie")
    ttr = fields.Integer(string="Tipo de Comprobante Fiscal")


class POSConfigWithFiscalPrinter(models.Model):
    _name = 'pos.config'
    _inherit = 'pos.config'

    fiscal_printer_id = fields.Many2one("neotec_interface.fiscal_printer", "Impresora Fiscal")


class CustomPartner(models.Model):
    _name = 'res.partner'
    _inherit = 'res.partner'

    ncf_type_id = fields.Many2one('neotec_interface.ncf_type', 'Tipo Comprobante')

    @api.onchange('vat')
    def get_rnc(self):
        try:
            if (self.vat):
                res = urllib2.urlopen('http://api.marcos.do/rnc/' + self.vat)
                if res.code == 200:
                    company = json.load(res)
                    if 'comercial_name' in company:
                        if (len(company['comercial_name']) != 1):
                            self.name = company['comercial_name']
                        else:
                            self.name = company['name']
        except urllib2.URLError as e:
            if hasattr(e, 'reason'):
                print 'We failed to reach a server.'
                print 'Reason: ', e.reason
            elif hasattr(e, 'code'):
                print 'The server couldn\'t fulfill the request.'
                print 'Error code: ', e.code
            return " "


class PaymentType(models.Model):
    _name = 'neotec_interface.payment_type'
    name = fields.Char(string=u"Título", required=True)
    account_journal_id = fields.Many2one("account.journal", string="Tipo de Pago")
    code = fields.Integer(string=u"Código", readonly=True)


class CustomPosOrder(models.Model):
    _inherit = 'pos.order'

    ncf = fields.Char(string="NCF", readonly=True)
    is_delivery_order = fields.Boolean(string="Orden Domicilio")
    delivery_address = fields.Char(string=u"Dirección")
    using_legal_tip = fields.Boolean(string='Incluir Propina Legal', readonly=True)
    legal_tip = fields.Float(string="Propina legal (10%)", compute='_calculate_legal_tip')

    @api.one
    @api.model
    def _calculate_legal_tip(self):
        order_lines = self.env['pos.order.line'].search([('order_id', '=', self.ids[0])])

        total = 0

        if self.using_legal_tip:
            for order_line in order_lines:
                total += order_line.price_subtotal

        legal_tip = total * 0.10;
        self.amount_total += legal_tip
        self.legal_tip = neoutil.round_to_2(legal_tip)


class CustomLegacyPosOrder(osv.osv):
    """
        Made for overriding the odoo 8 pos.order _amount_all method, so that legal tip is included in total
    """
    _name = 'pos.order'
    _inherit = 'pos.order'

    def _amount_all(self, cr, uid, ids, name, args, context=None):
        res = super(CustomLegacyPosOrder, self)._amount_all(cr, uid, ids, name, args, context)
        for order in self.browse(cr, uid, ids, context=context):
            amount_untaxed = 0
            for line in order.lines:
                amount_untaxed += line.price_subtotal

            cr.execute('SELECT using_legal_tip FROM pos_order WHERE id = ' + str(order.id))
            using_legal_tip = cr.fetchone()[0]

            if using_legal_tip:
                res[order.id]['amount_total'] += amount_untaxed * 0.10

        return res

    def test_paid(self, cr, uid, ids, context=None):
        """A Point of Sale is paid when the sum
        @return: True
        """
        for order in self.browse(cr, uid, ids, context=context):
            if order.lines and not order.amount_total:
                return True

            amount_untaxed = 0
            for line in order.lines:
                amount_untaxed += line.price_subtotal

            legal_tip = amount_untaxed * 0.10
            if (not order.lines) or (not order.statement_ids) or \
                    (neoutil.round_to_2(abs(order.amount_total - (order.amount_paid - legal_tip))) > 0.00001):
                return False
        return True

    _columns = {
        'amount_tax': oldField.function(_amount_all, string='Taxes', digits=0, multi='all'),
        'amount_total': oldField.function(_amount_all, string='Total', digits=0, multi='all'),
        'amount_paid': oldField.function(_amount_all, string='Paid', states={'draft': [('readonly', False)]}, readonly=True, digits=0, multi='all'),
        'amount_return': oldField.function(_amount_all, string='Returned', digits=0, multi='all'),
    }


class CustomPosOrderLine(models.Model):
    _name = 'pos.order.line'
    _inherit = 'pos.order.line'

    price_with_tax = fields.Float(string="Precio con Impuestos", compute="_calculate_price_with_tax")

    @api.one
    def _calculate_price_with_tax(self):

        tax_amount = 0
        if(self.tax_ids):
            tax_amount = self.tax_ids.amount

        t = self.price_unit * (tax_amount / 100)
        self.price_with_tax = neoutil.round_to_2(self.price_unit + t)
