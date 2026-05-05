<?php echo $link_tag1;?>
<?php echo $link_tag2;?>

	<style>
	.modal-header, h4, .close {
		background-color: #5cb85c;
		color:white !important;
		text-align: center;
		font-size: 25px;
	}
	.modal-footer {
		background-color: #f9f9f9;
	}
	#gview_jqGrid {overflow: hidden;}
	.ui-jqgrid tr.ui-jqgrid-labels th {background-color: #e8e8e8;}
	.ui-jqgrid tbody tr:hover {background-color: #e8e8e8;}
	.ui-jqgrid tr.jqgrow td {outline-style: none; color:#286abf;font-weight:normal; cursor : pointer; vertical-align:middle !important}
	.ui-jqgrid tr.jqgrow td { word-wrap: break-word; /* IE 5.5+ and CSS3 */ white-space: pre-wrap; /* CSS3 */ white-space: -moz-pre-wrap; /* Mozilla, since 1999 */ white-space: -pre-wrap; /* Opera 4-6 */ white-space: -o-pre-wrap; /* Opera 7 */ overflow: hidden; height: auto; vertical-align: middle; padding-top: 3px; padding-bottom: 3px; }
	/*.ui-jqgrid tr.jqgrow td { white-space: normal !important; height: auto; vertical-align: text-top; padding-top: 2px; }*/
	th.ui-th-column div {  word-wrap: break-word; /* IE 5.5+ and CSS3 */ white-space: pre-wrap; /* CSS3 */ white-space: -moz-pre-wrap; /* Mozilla, since 1999 */ white-space: -pre-wrap; /* Opera 4-6 */ white-space: -o-pre-wrap; /* Opera 7 */ overflow: hidden; height: auto; vertical-align: middle; padding-top: 3px; padding-bottom: 3px;  }
	.ui-jqgrid .ui-search-table { text-align: center; width: 90%; }
	.table>tbody>tr>td, .table>tfoot>tr>td, .table>thead>tr>td { padding: 2px 2px 2px 2px; }
	.ui-jqgrid td input { margin: 0 4px 0 8px; }
	.ui-jqgrid .ui-search-table td.ui-search-clear { display: none; }
	.ui-paging-info { padding-right: 20px; }
	.ui-jqgrid tr.ui-search-toolbar td > input { text-align: center; }
	</style>

<div id="page-wrapper">

	<div class="row">
		<div class="col-lg-12">
			<h3 class="page-header"><i class="fa fa-plus-square"></i> 상품코드 목록</h3>
		</div>
		<!-- /.col-lg-12 -->
	</div>

	<div class="row">
		<div class="col-lg-12">

			<?php if($auth_code == 15) { ?>
			<div class="panel panel-default">
				<div class="panel-heading">
					상품코드 등록
				</div>
				<!-- /.panel-heading -->
				<div class="panel-body">
					<div class="row">

						<form>
						<div class="col-lg-6">
							<div class="form-group <?php echo $goods_code_error;?>">
								<input type="text" name="goods_code" class="form-control" placeholder="상품코드" value="<?php echo set_value('goods_code');?>">
								<?php if(form_error('goods_code')) {?>
								<p class="help-block text-danger"><i class="fa fa-check"></i> <?php echo form_error('goods_code');?></p>
								<?php }?>
							</div>
						</div>
						<div class="col-lg-6">
							<div class="form-group">
								<button type="button" class="btn btn-block btn-primary" autocomplete="off">등록하기</button>
							</div>
						</div>
						</form>
					</div>
				</div>
				<!-- .panel-body -->
			</div>
			<!-- /.panel -->
			<?php }?>

			<div class="panel panel-default">
				<div class="panel-heading">
					상품코드 목록
				</div>
				<div class="table-responsive" style="overflow: hidden;">
					<section>
						<table id="jqGrid" class="table"></table>
						<div id="jqGridPager"></div>
					</section>
				</div>
				<div class="panel-footer">
				</div>
			</div>

		</div>
		<!-- /.col-lg-12 -->
	</div>

</div>
<!-- /#page-wrapper -->

<div class="modal fade" id="alertModal">
	<div class="modal-dialog modal-sm">
		<div class="modal-content">
			<div class="modal-header" style="padding:35px 50px;">
				<button type="button" class="close" data-dismiss="modal" aria-hidden="true">&times;</button>
				<h4 class="modal-title"><span class="glyphicon glyphicon-lock"></span> 경고창</h4>
			</div>
			<div class="modal-body">
				<div class="text-left">
					<h5></h5>
				</div>
			</div>
		</div>
	</div>
</div>

<div id="LoadingModal" class="modal" tabindex="-1" role="dialog" data-keyboard="false" data-backdrop="static">
	<div class="modal-dialog">
		<div class="modal-content">
			<div class="modal-header" style="text-align: center">
				<h3>처리중..</h3>
			</div>
			<div class="modal-body" >
				<div style="height:200px">
					<span id="loading_spinner_center" style="position: absolute;display: block;top: 50%;left: 50%;"></span>
				</div>
			</div>
			<div class="modal-footer" style="text-align: center">잠시만 기달려 주세요.</div>
		</div>
	</div>
</div>

<script type="text/javascript" language="javascript" src="/include/jqgrid/i18n/grid.locale-kr.js"></script>
<script type="text/javascript" language="javascript" src="/include/jqgrid/jquery.jqGrid.min.js"></script>
<script src="/assets/js/spin.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>

<script>
var LoadingModal = $('#LoadingModal');

$(function() 
{
	//$.jgrid.defaults.width = 780;
	$.jgrid.defaults.responsive = true;
	$.jgrid.defaults.styleUI = 'Bootstrap';

	$("#jqGrid").jqGrid({
		url: '/products/goods_code_ajax_list',
		mtype: "POST",
		datatype: "json",
		//width: 780,
		height: 600,
		colModel: [
			{ label: '일련번호', name: 'CodeId', key: true, hidden: true, editable: false, search:false },
			{ label: '기능', name: 'GoodsBtn', key: false, width: 100, align:'center', editable: false, sortable: false, formatter: ButtonValue, search:false },
			{ label: '상품코드', name: 'gcode', editable: false, width: 300, align:'center', search: true }, 
			{ label: '이미지수', name: 'GcodeCnt', editable: false, width: 60, align:'center', sortable: false, search: false }, 
			{ label: '등록일', name: 'created', width: 150, align:'center', search:true },
		],
		//page:1,
		//loadonce:true,
		//caption: '<h3><i class="fa fa-th-list"></i> 전체상품</h3>',
		rowNum: 20,
		rownumbers: true,
		//subGrid: true,
		//rownumWidth: 40,
		//gridview: true,
		autowidth: true,
		shrinkToFit: true, // 필드 width 를 responsive width 에 맞춘다.
		viewrecords: true,
		sortname: 'created',
		sortorder: "DESC",
		scroll: 1, // set the scroll property to 1 to enable paging with scrollbar - virtual loading of records
		//scrollrows: true,
		//hoverrows: true,
		gridview: true,        //처리속도를 빠르게 해준다. 시간측정시 절반가량 로딩시간 감소!!! 하지만 다음 모듈엔 사용할 수 없다!! ==> treeGrid, subGrid, afterInsertRow(event)
		multiselect:false,
		emptyrecords: '데이타가 없습니다.', // the message will be displayed at the bottom 
		pager: "#jqGridPager",
		subGrid: false,
		//subGridRowExpanded: showChildGrid,
		onSelectRow: function (rowId) {
			$("#jqGrid").jqGrid('toggleSubGridRow', rowId);
		},
		loadBeforeSend: function () {
			LoadingModal.modal('show');
			$(this).closest("div.ui-jqgrid-view").find("table.ui-jqgrid-htable>thead>tr>th").css("text-align", "center");
		},
		loadComplete: function () {
			LoadingModal.modal('hide');
		}
	});

	$("#jqGrid").jqGrid('navGrid','#jqGridPager',{edit:false, add:false, del:false, search: false});
	$("#jqGrid").jqGrid('filterToolbar', {stringResult: true, searchOnEnter: true, defaultSearch: 'cn', ignoreCase: true});

	function fixSearchOperators() {
		var $grid = $("#jqGrid"),
			columns = $grid.jqGrid ('getGridParam', 'colModel'),
			filterToolbar = $($grid[0].grid.hDiv).find("tr.ui-search-toolbar");

		filterToolbar.find("th").each(function(index) {
			var $searchOper = $(this).find(".ui-search-oper");
			if (!(columns[index].searchoptions && columns[index].searchoptions.searchOperators)) {
				$searchOper.hide();
			}
		});
	}

	function formatImage(cellValue, options, rowObject) 
	{
		//console.log(options);
		//console.log(typeof rowObject);
		//console.log(rowObject);

		//var res = cellValue.split(".");
		//var imageHtml = "<img src='" + res[0] + "_thumb." + res[1] + "' width='60px' height='60px' originalValue='" + cellValue + "' />";
		var imageHtml = "<img src='" + cellValue + "' width='60px' height='60px' originalValue='" + cellValue + "' onerror=\"this.onerror=null;this.style.outline='2px solid #ff3b30';this.title='이미지 없음(404)';\" />";
		return imageHtml;
	}
	
	function ButtonValue(cellvalue, options, rowObject) 
	{
		//console.log(rowObject);
		var link;
		var img_cnt = rowObject[3];

		// <button class="btn btn-success btn-xs btn-block" type="button" onclick="goodsProcess(\'' + rowObject[0] + '\', \'C\');">복사</button>
		if(img_cnt != '0')
			link = '<button class="btn btn-primary btn-lg" type="button" onclick="codeProcess(\'' + rowObject[2] + '\', \'E\');">관리</button> <button class="btn btn-warning btn-lg" type="button" onclick="codeProcess(\'' + rowObject[2] + '\', \'S\');">정렬</button> <button class="btn btn-warning btn-lg" type="button" onclick="codeProcess(\'' + rowObject[2] + '\', \'T\');">TEST</button> <button class="btn btn-danger btn-lg" type="button" onclick="codeProcess(\'' + rowObject[2] + '\', \'D\');">다운</button>';
		else
			link = '<button class="btn btn-primary btn-lg" type="button" onclick="codeProcess(\'' + rowObject[2] + '\', \'E\');">관리</button>';
		
		return link;
	}

	$('.form-group button').on('click', function () 
	{
		console.log(this.form.goods_code.value);

		if(!this.form.goods_code.value)
		{
			alert('등록할 상품코드를 입력하세요!');
			return;
		}

		location.href = '/products/goods_img/'+this.form.goods_code.value;

		//console.log($btn);
		//setTimeout(function(){$btn.button("reset")}, 1000);
		// business logic...
		//$btn.button('reset');
		//alert(market);
		//this.form.submit();

	});

	$('#no-more-tables button').on('click', function () 
	{
		//console.log(this.form);
		var val = $(this).attr('val');
		var res = val.split("|");
		console.log(res);

		var gcode = res[0];		// 상품코드
		var imgcnt = res[1];	// 상품코드 이미지수

		if(imgcnt == 0)
		{
			alert('해당 상품코드는 이미지가 없습니다!');
			return;
		}

		download_jszip(null, gcode);

	});

});

function download_jszip(goodsId, goodsCode) {
	if (typeof JSZip === 'undefined') {
		window.open('/products/goods_code_zip_down/' + goodsCode);
		return;
	}
	$.getJSON('/products/goods_zip_urls?code=' + goodsCode, function(data) {
		if (!data.success) { alert(data.msg || '다운로드 오류입니다.'); return; }
		var zip = new JSZip();
		var loaded = 0, total = data.images.length;
		var failed = [];
		var concurrency = 5;
		var cursor = 0;
		var active = 0;
		if (data.txt) zip.file(data.txt_name, data.txt);

		function fetchUrlWithRetry(url, retryLeft) {
			return fetch(url, {cache: 'no-store', credentials: 'same-origin'})
				.then(function(res) {
					if (!res.ok) throw new Error('HTTP ' + res.status);
					return res.arrayBuffer();
				})
				.catch(function(e) {
					if (retryLeft > 0) {
						return new Promise(function(resolve) {
							setTimeout(resolve, 500);
						}).then(function() {
							return fetchUrlWithRetry(url, retryLeft - 1);
						});
					}
					throw e;
				});
		}

		function fetchWithRetry(img, retryLeft) {
			return fetchUrlWithRetry(img.url, retryLeft)
				.catch(function(primaryError) {
					if (!img.fallback_url) throw primaryError;
					return fetchUrlWithRetry(img.fallback_url, 2)
						.catch(function(fallbackError) {
							throw new Error('primary: ' + (primaryError.message || primaryError) + ', fallback: ' + (fallbackError.message || fallbackError));
						});
				});
		}

		function logFailures() {
			if (!failed.length) return;
			$.ajax({
				url: '/products/goods_zip_download_log',
				type: 'POST',
				data: {
					code: goodsCode,
					expected: total,
					success: total - failed.length,
					failed: JSON.stringify(failed)
				}
			});
		}

		function finishZip() {
			if (failed.length) {
				var guide = '이미지 다운로드 안내\n\n';
				guide += '상품코드: ' + goodsCode + '\n';
				guide += '전체 이미지: ' + total + '개\n';
				guide += '다운로드 성공: ' + (total - failed.length) + '개\n';
				guide += '다운로드 실패: ' + failed.length + '개\n\n';
				guide += '일부 이미지가 네트워크 또는 CDN 응답 지연으로 포함되지 않았습니다.\n';
				guide += '잠시 후 다시 다운로드하시면 누락 파일을 받을 수 있습니다.\n\n';
				guide += '누락 파일:\n';
				failed.forEach(function(item) {
					guide += '- ' + item.zip_path + ' (' + item.error + ')\n';
				});
				zip.file('다운로드_안내.txt', guide);
				logFailures();
			}
			if (total > 0 && failed.length === total) {
				alert('브라우저 직접 다운로드가 차단되어 기존 방식으로 다시 시도합니다.');
				window.open('/products/goods_code_zip_down/' + goodsCode);
				return;
			}
			zip.generateAsync({type:'blob'}).then(function(blob) {
				var a = document.createElement('a');
				a.href = URL.createObjectURL(blob);
				a.download = failed.length
					? (data.partial_zip_name || ((data.goods_name || goodsCode) + '_partial_missing_' + failed.length + '.zip'))
					: (data.zip_name || goodsCode + '.zip');
				document.body.appendChild(a); a.click();
				document.body.removeChild(a);
				URL.revokeObjectURL(a.href);
				if (failed.length) alert('이미지 ' + failed.length + '개가 네트워크 응답 실패로 누락되었습니다. ZIP 안의 다운로드_안내.txt를 확인하고 잠시 후 다시 다운로드해 주세요.');
			});
		}

		function runQueue() {
			if (cursor >= total && active === 0) {
				finishZip();
				return;
			}
			while (active < concurrency && cursor < total) {
				(function(img) {
					active++;
					fetchWithRetry(img, 2)
						.then(function(buf) {
							zip.file(img.zip_path, buf);
						})
						.catch(function(e) {
							failed.push({
								url: img.url,
								zip_path: img.zip_path,
								error: e && e.message ? e.message : 'fetch failed'
							});
						})
						.then(function() {
							loaded++;
							active--;
							runQueue();
						});
				})(data.images[cursor++]);
			}
		}

		if (!total) {
			alert('다운로드할 이미지가 없습니다.');
			return;
		}
		runQueue();
	}).fail(function() { window.open('/products/goods_code_zip_down/' + goodsCode); });
}

function codeProcess(goodsCode, gb)
{
	if(!goodsCode || !gb)
	{
		alert("정상적인 접근이 아닙니다!");
		return;
	}

	// 수정
	if(gb == "E")
	{
		window.location.href = '/products/goods_img/'+goodsCode;
	}

	// 정렬
	if(gb == "S")
	{
		window.open('/products/goods_img_sorting/'+goodsCode, goodsCode, "width=1200,height=800,scrollbars=yes");
	}

	// 정렬
	if(gb == "T")
	{
		window.open('/products/goods_img_sorting_test/'+goodsCode, goodsCode, "width=1200,height=800,scrollbars=yes");
	}

	// 다운
	if(gb == "D")
	{
		download_jszip(null, goodsCode);
	}
}

// 등록 처리중 모달창 처리
var opts = {
	lines: 13, // The number of lines to draw
	length: 20, // The length of each line
	width: 10, // The line thickness
	radius: 30, // The radius of the inner circle
	corners: 1, // Corner roundness (0..1)
	rotate: 0, // The rotation offset
	direction: 1, // 1: clockwise, -1: counterclockwise
	color: '#000', // #rgb or #rrggbb or array of colors
	speed: 1, // Rounds per second
	trail: 60, // Afterglow percentage
	shadow: false, // Whether to render a shadow
	hwaccel: false, // Whether to use hardware acceleration
	className: 'spinner', // The CSS class to assign to the spinner
	zIndex: 2e9, // The z-index (defaults to 2000000000)
	top: 'auto', // Top position relative to parent in px
	left:'auto' // Left position relative to parent in px
};
var target = document.getElementById('loading_spinner_center');
var spinner = new Spinner(opts).spin(target);

</script>
